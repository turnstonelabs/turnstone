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

import subprocess
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.model_backend_auth import BackendAuthUnavailableError
from turnstone.core.model_turn import (
    ResolvedModelBinding,
    lane_call_client,
    resolve_model_binding,
)
from turnstone.core.providers import thinking_off_template_kwargs
from turnstone.core.providers._protocol import refuse_aborted_request
from turnstone.core.server_compat import merge_server_compat

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from turnstone.core.model_registry import ModelConfig

log = get_logger(__name__)

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

# Providers whose client speaks the OpenAI-SDK surface audio.py relies on
# (``client.audio.*`` for the transcription/speech endpoints, ``client.chat.
# completions.*`` with ``input_audio`` for the omni chat path).  Anthropic and
# anthropic-compatible (e.g. a vLLM Messages-API endpoint) use a different SDK
# whose protocol has NO audio content block, so they can't serve audio in ANY
# role — gated out here.  Mirrors the OpenAI-lane split in
# turnstone.core.providers and the JS ``_providerCarriesAudio`` in admin.js.
_AUDIO_SDK_PROVIDERS: frozenset[str] = frozenset({"openai", "openai-compatible", "google", "xai"})

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

# Default instruction for transcribing via an omni *chat* model (one that accepts
# audio in chat but doesn't serve the dedicated /audio/transcriptions endpoint).
# Steers the model to emit only the transcript; an operator can override it
# per-deployment with the ``audio.stt_prompt`` setting.
_OMNI_STT_PROMPT = (
    "Transcribe the following speech segment in its original language. Follow these "
    "specific instructions for formatting the answer:\n"
    "* Only output the transcription, with no newlines.\n"
    "* When transcribing numbers, write the digits, i.e. write 1.7 and not one point "
    "seven, and write 3 instead of three"
)

# Bound the omni STT decode.  Gemma caps audio at 30 s and a 30 s transcript is
# well under this, so the cap only catches a pathological runaway — it never
# truncates a real transcript.
_OMNI_STT_MAX_TOKENS = 1024

# Hard limit on the ffmpeg transcode subprocess (seconds).
_FFMPEG_TIMEOUT_S = 30

# Cap the decoded audio duration so a crafted clip can't expand into an
# unbounded decode (the upload itself is already size-capped at the endpoint).
_MAX_AUDIO_SECONDS = 300

# Per-request timeout for the streaming STT chat call — bounds a hung backend
# (the whole transcription is ~1 s; this only catches a stalled stream).
_OMNI_STT_TIMEOUT_S = 60


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


def _resolve_audio_binding(
    *,
    registry: Any,
    alias: str,
    config_store: Any | None = None,
    backend_auth_resolver: Callable[[str, ModelConfig | None], str | None] | None = None,
) -> ResolvedModelBinding:
    """Resolve one coherent registry generation for an audio role call."""
    try:
        binding = resolve_model_binding(
            registry,
            alias,
            config_store=config_store,
            backend_auth_resolver=backend_auth_resolver,
        )
    except Exception as exc:
        raise AudioUnavailableError(f"Audio model alias {alias!r} is not available") from exc
    if binding.config is None:
        raise AudioUnavailableError(f"Audio model alias {alias!r} has no model definition")
    return binding


def _capture_audio_handle(cancel_ref: Any, handle: Any) -> None:
    """Publish a closeable raw response/stream to the caller's abort ref."""
    if cancel_ref is not None:
        cancel_ref.append(handle)
    refuse_aborted_request(cancel_ref)


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


def _provider_carries_audio(cfg: Any) -> bool:
    """Whether the alias's provider can carry audio over the OpenAI-SDK surface."""
    return getattr(cfg, "provider", "openai") in _AUDIO_SDK_PROVIDERS


def model_supports_role(cfg: Any, role: str) -> bool:
    """Whether the alias's model is eligible for a media *role*.

    Explicit ``capabilities[<flag>]`` wins; otherwise fall back to a
    known-model-name inference for OpenAI audio models.  Either way the
    provider must speak the OpenAI-SDK audio surface (see
    :data:`_AUDIO_SDK_PROVIDERS`) — an Anthropic(-compatible) model can't carry
    audio in any role even if a capability flag is ticked.
    """
    flag = _ROLE_CAPABILITY.get(role)
    if not flag:
        return False
    if not _provider_carries_audio(cfg):
        return False
    caps = getattr(cfg, "capabilities", None) or {}
    # An omni model (accepts audio in chat) can serve STT via the chat
    # transcription path even without the dedicated /audio/transcriptions
    # endpoint — see :func:`transcribe`.  Mirrored in admin.js _audioModelEligible.
    if role == "stt" and caps.get("supports_audio_input"):
        return True
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


def _serves_transcription_endpoint(cfg: Any, model: str) -> bool:
    """Whether the alias serves the dedicated ``/audio/transcriptions`` endpoint
    (whisper-style), as opposed to an omni chat model that ingests audio inline.

    Explicit ``supports_transcription`` wins; otherwise infer from the model name.
    """
    caps = getattr(cfg, "capabilities", None) or {}
    if "supports_transcription" in caps:
        return bool(caps["supports_transcription"])
    return _infer_audio_capability(model, "stt")


def _to_wav_16k_mono(data: bytes) -> bytes:
    """Decode any ffmpeg-readable audio container to 16 kHz mono PCM WAV.

    Browsers record webm/opus (or ogg/mp4); the omni chat lane — vLLM in
    particular — only decodes wav/mp3 and sniffs the bytes, so the raw upload is
    rejected as an "Invalid or unsupported audio file".  ffmpeg reads the
    container from the byte stream (no reliance on the filename) and resamples to
    the 16 kHz mono PCM the model documents.  Raises :class:`AudioBackendError`
    (the endpoint maps it to 502) if ffmpeg is missing or the bytes don't decode.
    """
    # ffmpeg reads only the piped bytes (-protocol_whitelist pipe) so a crafted
    # container can't open file:/http: references (SSRF / local file read); -vn
    # drops video streams and -t bounds the decode against a decompression bomb.
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-i",
                "pipe:0",
                "-vn",
                "-t",
                str(_MAX_AUDIO_SECONDS),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise AudioBackendError("ffmpeg is not installed; cannot transcode audio") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioBackendError("Audio transcode timed out") from exc
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise AudioBackendError(f"Audio transcode failed: {detail[-200:] or 'no output'}")
    return proc.stdout


def _omni_chat_extra_body(cfg: Any) -> dict[str, Any]:
    """Build the chat ``extra_body`` for an omni STT call.

    The STT path calls the raw client, so it bypasses the provider's request
    shaping.  Reuse ``merge_server_compat`` to forward any operator-stored
    ``server_compat["extra_body"]``, then force **thinking OFF** via
    :func:`thinking_off_template_kwargs` — THE shared spelling of "this lane
    needs no reasoning", also used by the drained utility completions:
    transcription needs no reasoning, and leaving it on multiplies latency
    ~10x and (on some chat templates) empties the content.  The override is
    applied last so it wins over any operator thinking flag.
    """
    server_compat = getattr(cfg, "server_compat", None)
    extra = merge_server_compat(None, server_compat) if isinstance(server_compat, dict) else {}
    caps = getattr(cfg, "capabilities", None) or {}
    off = thinking_off_template_kwargs(
        str(caps.get("thinking_mode") or ""), str(caps.get("thinking_param") or "")
    )
    if off:
        extra.setdefault("chat_template_kwargs", {}).update(off)
    return extra


def _omni_chat_messages(prompt: str, audio_b64: str) -> list[dict[str, Any]]:
    """The single user turn for an omni STT chat call: the prompt precedes the
    audio part — the order Gemma documents for transcription."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}},
            ],
        }
    ]


def _transcribe_via_chat(
    binding: ResolvedModelBinding,
    data: bytes,
    prompt: str,
    *,
    extra_body: dict[str, Any] | None = None,
    max_tokens: int = _OMNI_STT_MAX_TOKENS,
    cancel_ref: Any = None,
) -> str:
    """Transcribe by handing the clip to an omni *chat* model as ``input_audio``.

    For models that accept audio in chat (``supports_audio_input``) but don't
    serve ``/audio/transcriptions``.  The clip is transcoded to 16 kHz mono WAV
    first (browsers record webm/opus, which the chat lane can't decode).  The
    instruction ``prompt`` precedes the audio part — the order Gemma documents
    for transcription — and ``extra_body`` carries the thinking-off / server
    compat params the raw-client path would otherwise skip.
    """
    import base64

    wav = _to_wav_16k_mono(data)
    audio_b64 = base64.b64encode(wav).decode("ascii")
    lane = binding.lane
    client = lane_call_client(lane, cancel_ref=cancel_ref)
    manager = client.chat.completions.with_streaming_response.create(
        model=lane.model,
        messages=_omni_chat_messages(prompt, audio_b64),
        max_tokens=max_tokens,
        extra_body=extra_body or None,
    )
    refuse_aborted_request(cancel_ref)
    with manager as response:
        _capture_audio_handle(cancel_ref, response)
        resp = response.parse()
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return ""
    return (getattr(choices[0].message, "content", "") or "").strip()


def _transcribe_binding(
    binding: ResolvedModelBinding,
    *,
    data: bytes,
    filename: str,
    prompt: str = "",
    cancel_ref: Any = None,
) -> TranscriptionResult:
    """Transcribe through one already-resolved binding generation."""
    lane = binding.lane
    cfg = binding.config
    if cfg is None:
        raise AudioUnavailableError(f"STT model alias {lane.alias!r} has no model definition")
    model = lane.model
    alias = lane.alias
    if not _provider_carries_audio(cfg):
        raise AudioUnavailableError(
            f"STT model alias {alias!r} (provider {cfg.provider!r}) can't transcribe audio — "
            "audio roles require an OpenAI-compatible provider."
        )
    caps = cfg.capabilities or {}
    endpoint = _serves_transcription_endpoint(cfg, model)
    if not endpoint and not caps.get("supports_audio_input"):
        raise AudioUnavailableError(f"STT model alias {alias!r} cannot transcribe audio")
    refuse_aborted_request(cancel_ref)
    try:
        if endpoint:
            client = lane_call_client(lane, cancel_ref=cancel_ref)
            kwargs: dict[str, Any] = {
                "model": model,
                "file": (filename or "speech.webm", data),
                "response_format": "json",
            }
            if prompt:
                kwargs["prompt"] = prompt
            manager = client.audio.transcriptions.with_streaming_response.create(**kwargs)
            refuse_aborted_request(cancel_ref)
            with manager as response:
                _capture_audio_handle(cancel_ref, response)
                resp = response.parse()
            transcript = (getattr(resp, "text", "") or "").strip()
        else:
            transcript = _transcribe_via_chat(
                binding,
                data,
                prompt or _OMNI_STT_PROMPT,
                extra_body=_omni_chat_extra_body(cfg),
                cancel_ref=cancel_ref,
            )
    except (AudioBackendError, BackendAuthUnavailableError):
        raise
    except Exception as exc:
        refuse_aborted_request(cancel_ref)
        raise AudioBackendError(f"Transcription backend failed: {exc}") from exc
    refuse_aborted_request(cancel_ref)
    return TranscriptionResult(transcript=transcript, model_alias=alias, model=model)


def transcribe(
    *,
    registry: Any,
    alias: str,
    data: bytes,
    filename: str,
    prompt: str = "",
    config_store: Any | None = None,
    backend_auth_resolver: Callable[[str, ModelConfig | None], str | None] | None = None,
    cancel_ref: Any = None,
) -> TranscriptionResult:
    """Transcribe ``data`` using the STT role alias's audio backend.

    A whisper-style alias (``supports_transcription`` / a transcription model
    name) goes through ``/audio/transcriptions``; an omni alias
    (``supports_audio_input``) transcribes via chat ``input_audio`` instead.

    ``prompt`` (when non-empty) is forwarded as the transcription ``prompt`` on
    the endpoint path (vocabulary bias) and as the instruction on the chat path;
    the chat path falls back to :data:`_OMNI_STT_PROMPT` so a bare omni call still
    emits a clean transcript rather than a conversational reply.
    """
    binding = _resolve_audio_binding(
        registry=registry,
        alias=alias,
        config_store=config_store,
        backend_auth_resolver=backend_auth_resolver,
    )
    return _transcribe_binding(
        binding,
        data=data,
        filename=filename,
        prompt=prompt,
        cancel_ref=cancel_ref,
    )


def _iter_stream_deltas(stream: Any, *, cancel_ref: Any = None) -> Iterator[str]:
    """Yield non-empty content deltas from an OpenAI streaming chat response.

    Owns the stream's lifecycle: exhausting or closing this generator releases
    the underlying HTTP connection, so an abandoned stream can't leak it.
    """
    try:
        try:
            refuse_aborted_request(cancel_ref)
            for chunk in stream:
                refuse_aborted_request(cancel_ref)
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0].delta, "content", None)
                if delta:
                    yield delta
            refuse_aborted_request(cancel_ref)
        except Exception:
            refuse_aborted_request(cancel_ref)
            raise
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()


def transcribe_stream(
    *,
    registry: Any,
    alias: str,
    data: bytes,
    prompt: str = "",
    config_store: Any | None = None,
    backend_auth_resolver: Callable[[str, ModelConfig | None], str | None] | None = None,
    cancel_ref: Any = None,
) -> Iterator[str]:
    """Stream transcript content deltas for the STT role alias.

    Resolve, transcode, and opening the streaming-chat request all run eagerly
    (before the returned generator yields its first delta) so the caller can
    surface a clean 503 / 502; only the token iteration is deferred.  A
    whisper-style endpoint alias has no chat stream, so it emits the whole
    transcript as a single chunk.
    """
    binding = _resolve_audio_binding(
        registry=registry,
        alias=alias,
        config_store=config_store,
        backend_auth_resolver=backend_auth_resolver,
    )
    lane = binding.lane
    cfg = binding.config
    if cfg is None:
        raise AudioUnavailableError(f"STT model alias {lane.alias!r} has no model definition")
    model = lane.model
    alias = lane.alias
    if not _provider_carries_audio(cfg):
        raise AudioUnavailableError(
            f"STT model alias {alias!r} (provider {getattr(cfg, 'provider', 'unknown')!r}) "
            "can't transcribe audio — audio roles require an OpenAI-compatible provider."
        )
    if _serves_transcription_endpoint(cfg, model):
        # Whisper-style endpoint: no chat stream — emit the whole transcript once.
        text = _transcribe_binding(
            binding,
            data=data,
            filename="speech.webm",
            prompt=prompt,
            cancel_ref=cancel_ref,
        ).transcript
        return iter([text] if text else [])
    caps = getattr(cfg, "capabilities", None) or {}
    if not caps.get("supports_audio_input"):
        raise AudioUnavailableError(f"STT model alias {alias!r} cannot transcribe audio")

    import base64

    wav = _to_wav_16k_mono(data)
    audio_b64 = base64.b64encode(wav).decode("ascii")
    try:
        client = lane_call_client(lane, cancel_ref=cancel_ref)
        request_kwargs = {
            "model": model,
            "messages": _omni_chat_messages(prompt or _OMNI_STT_PROMPT, audio_b64),
            "max_tokens": _OMNI_STT_MAX_TOKENS,
            "extra_body": _omni_chat_extra_body(cfg) or None,
            "stream": True,
            "timeout": _OMNI_STT_TIMEOUT_S,
        }
        refuse_aborted_request(cancel_ref)
        stream = client.chat.completions.create(**request_kwargs)
        _capture_audio_handle(cancel_ref, stream)
    except BackendAuthUnavailableError:
        raise
    except Exception as exc:
        refuse_aborted_request(cancel_ref)
        raise AudioBackendError(f"Transcription backend failed: {exc}") from exc
    return _iter_stream_deltas(stream, cancel_ref=cancel_ref)


# -- transcript memoization (no-native-audio wire fallback) -------------------
# Caching an STT result is an audio-domain concern, so it lives here next to
# ``transcribe``.  The wire resolver re-materializes every attachment on every
# send, so without this an audio clip attached early in a conversation would be
# re-sent to the (external, fallible) STT backend on every subsequent turn.
_TRANSCRIPT_CACHE_MAX = 256
_transcript_lock = threading.Lock()
_transcript_cache: dict[tuple[str, str, int, str], str] = {}


def _clear_transcript_cache_for_test() -> None:
    with _transcript_lock:
        _transcript_cache.clear()


def transcribe_cached(
    *,
    registry: Any,
    alias: str,
    content_hash: str,
    data: bytes,
    filename: str,
    principal_id: str = "",
    config_store: Any | None = None,
    backend_auth_resolver: Callable[[str, ModelConfig | None], str | None] | None = None,
    cancel_ref: Any = None,
) -> str:
    """Memoized, non-raising :func:`transcribe` for the wire fallback.

    Keyed by principal, concrete alias, registry generation, and content hash.
    Returns ``""`` on an ordinary backend failure (a placeholder is rendered
    upstream) and does *not* cache failures, so a transient outage doesn't
    poison the memo.  Cancellation and dynamic-auth refusal remain control flow
    and propagate to the caller.
    """
    refuse_aborted_request(cancel_ref)
    try:
        binding = _resolve_audio_binding(
            registry=registry,
            alias=alias,
            config_store=config_store,
            backend_auth_resolver=backend_auth_resolver,
        )
    except AudioUnavailableError as exc:
        refuse_aborted_request(cancel_ref)
        log.warning("audio transcription fallback failed: %s", exc)
        return ""
    refuse_aborted_request(cancel_ref)
    key = (
        principal_id.strip(),
        binding.lane.alias,
        binding.registry_generation,
        content_hash,
    )
    with _transcript_lock:
        cached = _transcript_cache.get(key)
        cache_hit = key in _transcript_cache
    if cache_hit:
        refuse_aborted_request(cancel_ref)
        return cached or ""
    try:
        text = _transcribe_binding(
            binding,
            data=data,
            filename=filename,
            cancel_ref=cancel_ref,
        ).transcript
    except (AudioUnavailableError, AudioBackendError) as exc:
        refuse_aborted_request(cancel_ref)
        log.warning("audio transcription fallback failed: %s", exc)
        return ""
    refuse_aborted_request(cancel_ref)
    with _transcript_lock:
        refuse_aborted_request(cancel_ref)
        # The backend call ran unlocked.  Preserve a real result a concurrent
        # caller already memoized instead of letting a slower empty completion
        # pin the placeholder for this principal/binding generation.
        existing = _transcript_cache.get(key)
        if existing:
            return existing
        if key not in _transcript_cache and len(_transcript_cache) >= _TRANSCRIPT_CACHE_MAX:
            _transcript_cache.pop(next(iter(_transcript_cache)), None)
        _transcript_cache[key] = text
    return text


def synthesize(
    *,
    registry: Any,
    alias: str,
    text: str,
    voice: str,
    response_format: str = "mp3",
    config_store: Any | None = None,
    backend_auth_resolver: Callable[[str, ModelConfig | None], str | None] | None = None,
    cancel_ref: Any = None,
) -> SpeechResult:
    """Synthesize ``text`` to speech using the TTS role alias's audio backend."""
    binding = _resolve_audio_binding(
        registry=registry,
        alias=alias,
        config_store=config_store,
        backend_auth_resolver=backend_auth_resolver,
    )
    lane = binding.lane
    cfg = binding.config
    if cfg is None:
        raise AudioUnavailableError(f"TTS model alias {lane.alias!r} has no model definition")
    model = lane.model
    alias = lane.alias
    if not model_supports_role(cfg, "tts"):
        raise AudioUnavailableError(f"TTS model alias {alias!r} cannot synthesize speech")
    refuse_aborted_request(cancel_ref)
    try:
        client = lane_call_client(lane, cancel_ref=cancel_ref)
        manager = client.audio.speech.with_streaming_response.create(
            model=model,
            voice=voice or _DEFAULT_VOICE,
            input=text,
            response_format=response_format,
        )
        refuse_aborted_request(cancel_ref)
        with manager as response:
            _capture_audio_handle(cancel_ref, response)
            audio_bytes = response.read()
    except BackendAuthUnavailableError:
        raise
    except Exception as exc:
        refuse_aborted_request(cancel_ref)
        raise AudioBackendError(f"TTS backend failed: {exc}") from exc
    refuse_aborted_request(cancel_ref)
    return SpeechResult(
        audio_bytes=audio_bytes,
        media_type=_MEDIA_TYPES.get(response_format, "audio/mpeg"),
        model_alias=alias,
        model=model,
    )
