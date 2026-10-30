"""Unit tests for the STT/TTS audio helper (model-role resolution + backends).

Most tests exercise ``transcribe`` / ``synthesize`` through the registry
boundary with a mocked OpenAI-SDK client. HTTP deadline regressions also run
the real SDK and production transport against a local server.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import openai
import pytest

import turnstone.core.admission as admission_mod
from tests._helpers import slot_held, wait_until
from turnstone.core import audio
from turnstone.core.admission import ModelAdmission
from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef
from turnstone.core.model_backend_auth import BackendAuthUnavailableError
from turnstone.core.providers import _http_client as provider_http
from turnstone.core.providers._http_client import shutdown_on_close
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider


class _Cfg:
    """Stand-in for ModelConfig — only the fields audio.py reads."""

    def __init__(
        self,
        model: str,
        capabilities: dict | None = None,
        provider: str = "openai",
        server_compat: dict | None = None,
    ) -> None:
        self.model = model
        self.capabilities = capabilities or {}
        self.provider = provider
        self.server_compat = server_compat or {}


class _FakeConfigStore:
    def __init__(self, **values: str) -> None:
        self._values = values

    def get(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)


class _FakeRegistry:
    """Minimal registry exposing the surface audio.py uses.

    With an ``admission`` gate it answers the six-value binding a real registry
    gives, so audio calls take that gate's slots.
    """

    def __init__(
        self,
        alias: str,
        cfg: _Cfg,
        client: object,
        admission: ModelAdmission | None = None,
    ) -> None:
        self._alias = alias
        self._cfg = cfg
        self._client = client
        self._admission = admission
        self.default = alias
        self.generation = 0
        self.resolve_binding_calls = 0
        self._provider = OpenAIChatCompletionsProvider()

    def has_alias(self, alias: str) -> bool:
        return alias == self._alias

    def get_config(self, alias: str) -> _Cfg:
        if alias != self._alias:
            raise ValueError(alias)
        return self._cfg

    def resolve_binding(self, alias: str | None = None):
        if alias not in (None, self._alias):
            raise ValueError(alias)
        self.resolve_binding_calls += 1
        if self._admission is not None:
            return (
                self._client,
                self._cfg.model,
                self._cfg,
                self._provider,
                self._admission,
                self.generation,
            )
        return self._client, self._cfg.model, self._cfg, self._provider, self.generation


def _response_manager(*, parsed=None, body: bytes = b""):
    response = MagicMock()
    response.parse.return_value = parsed
    response.read.return_value = body
    manager = MagicMock()
    manager.__enter__.return_value = response
    manager.__exit__.return_value = False
    return manager, response


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------


class TestModelSupportsRole:
    def test_explicit_flag_wins(self):
        assert audio.model_supports_role(_Cfg("anything", {"supports_transcription": True}), "stt")
        # Explicit False overrides the would-be inference from the model name.
        assert not audio.model_supports_role(
            _Cfg("gpt-4o-mini-tts", {"supports_speech_synthesis": False}), "tts"
        )

    def test_infers_known_openai_audio_models(self):
        assert audio.model_supports_role(_Cfg("gpt-4o-mini-transcribe"), "stt")
        assert audio.model_supports_role(_Cfg("whisper-1"), "stt")
        assert audio.model_supports_role(_Cfg("gpt-4o-mini-tts"), "tts")
        assert audio.model_supports_role(_Cfg("tts-1"), "tts")

    def test_omni_audio_input_eligible_for_stt(self):
        # An omni model (chat audio input) qualifies for STT via the chat path,
        # even with no transcription endpoint and a non-whisper name.
        assert audio.model_supports_role(_Cfg("gemma-omni", {"supports_audio_input": True}), "stt")
        # Audio *input* alone does not make it a TTS (speech-synthesis) model.
        assert not audio.model_supports_role(
            _Cfg("gemma-omni", {"supports_audio_input": True}), "tts"
        )

    def test_anthropic_provider_excluded_from_audio_roles(self):
        # Anthropic(-compatible) has no audio content block, so it can't serve
        # any audio role — even with a capability flag or a whisper-style name.
        assert not audio.model_supports_role(
            _Cfg("gemma-omni", {"supports_audio_input": True}, provider="anthropic-compatible"),
            "stt",
        )
        assert not audio.model_supports_role(
            _Cfg("whisper-1", provider="anthropic-compatible"), "stt"
        )
        assert not audio.model_supports_role(
            _Cfg("voice", {"supports_speech_synthesis": True}, provider="anthropic"), "tts"
        )

    def test_chat_model_not_eligible(self):
        assert not audio.model_supports_role(_Cfg("gpt-5"), "stt")
        # Anthropic has no audio API — gated out of every audio role.
        assert not audio.model_supports_role(_Cfg("claude-opus-4-8"), "tts")
        assert not audio.model_supports_role(_Cfg("claude-opus-4-8"), "stt")

    def test_unknown_role(self):
        assert not audio.model_supports_role(_Cfg("whisper-1"), "vision_eval")

    def test_hint_seed_lists_are_pinned(self):
        # Mirrored verbatim in admin.js AUDIO_MODEL_HINTS — if these change,
        # update the JS dropdown gate too (this pin makes the change deliberate).
        assert audio._AUDIO_MODEL_HINTS == {
            "stt": ("transcribe", "whisper", "-asr"),
            "tts": ("tts-", "-tts"),
        }


# ---------------------------------------------------------------------------
# Role resolution
# ---------------------------------------------------------------------------


class TestResolveRoleAlias:
    def test_resolves_configured_capable_alias(self):
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-transcribe"), MagicMock())
        cs = _FakeConfigStore(**{"audio.stt_model_alias": "voice"})
        assert audio.resolve_role_alias(config_store=cs, registry=reg, role="stt") == "voice"

    def test_none_when_unset(self):
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-transcribe"), MagicMock())
        assert (
            audio.resolve_role_alias(config_store=_FakeConfigStore(), registry=reg, role="stt")
            is None
        )

    def test_none_when_alias_missing_from_registry(self):
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-transcribe"), MagicMock())
        cs = _FakeConfigStore(**{"audio.stt_model_alias": "ghost"})
        assert audio.resolve_role_alias(config_store=cs, registry=reg, role="stt") is None

    def test_none_when_alias_not_capability_eligible(self):
        # Alias exists but its model can't do TTS -> gated out (Anthropic case).
        reg = _FakeRegistry("brain", _Cfg("claude-opus-4-8"), MagicMock())
        cs = _FakeConfigStore(**{"audio.tts_model_alias": "brain"})
        assert audio.resolve_role_alias(config_store=cs, registry=reg, role="tts") is None

    def test_none_when_no_registry_or_store(self):
        assert audio.resolve_role_alias(config_store=None, registry=None, role="stt") is None


# ---------------------------------------------------------------------------
# transcribe / synthesize — boundary: mocked OpenAI-SDK client
# ---------------------------------------------------------------------------


class TestTranscribe:
    def test_calls_audio_transcriptions_and_returns_text(self):
        client = MagicMock()
        manager, _response = _response_manager(parsed=MagicMock(text="  hello world  "))
        client.audio.transcriptions.with_streaming_response.create.return_value = manager
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-transcribe"), client)
        res = audio.transcribe(
            registry=reg, alias="voice", data=b"RIFFfake", filename="speech.webm"
        )
        assert res.transcript == "hello world"
        assert res.model_alias == "voice"
        assert res.model == "gpt-4o-mini-transcribe"
        kwargs = client.audio.transcriptions.with_streaming_response.create.call_args.kwargs
        assert kwargs["model"] == "gpt-4o-mini-transcribe"
        assert kwargs["file"] == ("speech.webm", b"RIFFfake")

    def test_prompt_forwarded_when_set(self):
        client = MagicMock()
        manager, _response = _response_manager(parsed=MagicMock(text="ok"))
        client.audio.transcriptions.with_streaming_response.create.return_value = manager
        reg = _FakeRegistry("voice", _Cfg("whisper-1"), client)
        audio.transcribe(
            registry=reg, alias="voice", data=b"x", filename="a.wav", prompt="ACME jargon"
        )
        kwargs = client.audio.transcriptions.with_streaming_response.create.call_args.kwargs
        assert kwargs["prompt"] == "ACME jargon"

    def test_prompt_omitted_when_blank(self):
        client = MagicMock()
        manager, _response = _response_manager(parsed=MagicMock(text="ok"))
        client.audio.transcriptions.with_streaming_response.create.return_value = manager
        reg = _FakeRegistry("voice", _Cfg("whisper-1"), client)
        audio.transcribe(registry=reg, alias="voice", data=b"x", filename="a.wav")
        kwargs = client.audio.transcriptions.with_streaming_response.create.call_args.kwargs
        assert "prompt" not in kwargs

    def test_backend_failure_raises_backend_error(self):
        client = MagicMock()
        client.audio.transcriptions.with_streaming_response.create.side_effect = RuntimeError(
            "boom"
        )
        reg = _FakeRegistry("voice", _Cfg("whisper-1"), client)
        with pytest.raises(audio.AudioBackendError):
            audio.transcribe(registry=reg, alias="voice", data=b"x", filename="a.wav")

    def test_omni_model_transcribes_via_chat(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        client = MagicMock()
        msg = MagicMock(content="  the transcript  ")
        manager, _response = _response_manager(parsed=MagicMock(choices=[MagicMock(message=msg)]))
        client.chat.completions.with_streaming_response.create.return_value = manager
        reg = _FakeRegistry("omni", _Cfg("gemma-omni", {"supports_audio_input": True}), client)
        res = audio.transcribe(
            registry=reg, alias="omni", data=b"webmbytes", filename="speech.webm"
        )
        assert res.transcript == "the transcript"
        # The dedicated transcription endpoint is NOT used for an omni model.
        client.audio.transcriptions.with_streaming_response.create.assert_not_called()
        kwargs = client.chat.completions.with_streaming_response.create.call_args.kwargs
        parts = kwargs["messages"][0]["content"]
        # Prompt precedes the audio part — the order Gemma documents for transcription.
        assert [p["type"] for p in parts] == ["text", "input_audio"]
        # The clip is transcoded to wav regardless of the upload container.
        audio_part = next(p for p in parts if p["type"] == "input_audio")
        assert audio_part["input_audio"]["format"] == "wav"
        # A blank prompt falls back to the omni STT default instruction.
        text_part = next(p for p in parts if p["type"] == "text")
        assert "Only output the transcription" in text_part["text"]

    def test_omni_prompt_override_used(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        client = MagicMock()
        manager, _response = _response_manager(
            parsed=MagicMock(choices=[MagicMock(message=MagicMock(content="x"))])
        )
        client.chat.completions.with_streaming_response.create.return_value = manager
        reg = _FakeRegistry("omni", _Cfg("gemma-omni", {"supports_audio_input": True}), client)
        audio.transcribe(
            registry=reg, alias="omni", data=b"x", filename="a.wav", prompt="custom instruction"
        )
        kwargs = client.chat.completions.with_streaming_response.create.call_args.kwargs
        parts = kwargs["messages"][0]["content"]
        text_part = next(p for p in parts if p["type"] == "text")
        assert text_part["text"] == "custom instruction"

    def test_non_audio_provider_raises_clear_error(self):
        # A stale config could still point STT at an anthropic-compatible model
        # (no audio surface): fail with an actionable message, not an opaque
        # ``'Anthropic' object has no attribute 'chat'``.
        client = MagicMock()
        reg = _FakeRegistry(
            "omni",
            _Cfg("gemma", {"supports_audio_input": True}, provider="anthropic-compatible"),
            client,
        )
        with pytest.raises(audio.AudioUnavailableError, match="OpenAI-compatible provider"):
            audio.transcribe(registry=reg, alias="omni", data=b"x", filename="a.webm")
        client.chat.completions.with_streaming_response.create.assert_not_called()

    def test_dedicated_endpoint_uses_authenticated_clone_and_pinned_config(self):
        base_client = MagicMock()
        call_client = MagicMock()
        base_client.with_options.return_value = call_client
        manager, _response = _response_manager(parsed=MagicMock(text="hello"))
        call_client.audio.transcriptions.with_streaming_response.create.return_value = manager
        cfg = _Cfg("whisper-1")
        resolver = MagicMock(return_value="minted-token")

        result = audio.transcribe(
            registry=_FakeRegistry("voice", cfg, base_client),
            alias="voice",
            data=b"x",
            filename="a.wav",
            backend_auth_resolver=resolver,
        )

        assert result.transcript == "hello"
        resolver.assert_called_once_with("voice", cfg)
        base_client.with_options.assert_called_once_with(api_key="minted-token")
        base_client.audio.transcriptions.with_streaming_response.create.assert_not_called()
        base_client.close.assert_not_called()
        call_client.close.assert_not_called()

    def test_omni_endpoint_uses_authenticated_clone_and_pinned_config(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        base_client = MagicMock()
        call_client = MagicMock()
        base_client.with_options.return_value = call_client
        manager, _response = _response_manager(
            parsed=MagicMock(choices=[MagicMock(message=MagicMock(content="hello from omni"))])
        )
        call_client.chat.completions.with_streaming_response.create.return_value = manager
        cfg = _Cfg("omni", {"supports_audio_input": True})
        resolver = MagicMock(return_value="minted-token")

        result = audio.transcribe(
            registry=_FakeRegistry("voice", cfg, base_client),
            alias="voice",
            data=b"x",
            filename="a.webm",
            backend_auth_resolver=resolver,
        )

        assert result.transcript == "hello from omni"
        resolver.assert_called_once_with("voice", cfg)
        base_client.with_options.assert_called_once_with(api_key="minted-token")
        base_client.chat.completions.with_streaming_response.create.assert_not_called()
        base_client.close.assert_not_called()
        call_client.close.assert_not_called()

    def test_abort_during_response_parse_closes_handle_and_propagates(self):
        client = MagicMock()
        ref = StreamAbortRef()
        manager, response = _response_manager()

        def _abort_while_parsing():
            ref.abort()
            return MagicMock(text="too late")

        response.parse.side_effect = _abort_while_parsing
        client.audio.transcriptions.with_streaming_response.create.return_value = manager

        with pytest.raises(DeadlineCancelledError):
            audio.transcribe(
                registry=_FakeRegistry("voice", _Cfg("whisper-1"), client),
                alias="voice",
                data=b"x",
                filename="a.wav",
                cancel_ref=ref,
            )

        response.close.assert_called()

    def test_abort_after_transcription_manager_creation_prevents_dispatch(self):
        client = MagicMock()
        ref = StreamAbortRef()
        manager, response = _response_manager(parsed=MagicMock(text="too late"))

        def _create_manager(**_kwargs):
            ref.abort()
            return manager

        client.audio.transcriptions.with_streaming_response.create.side_effect = _create_manager

        with pytest.raises(DeadlineCancelledError):
            audio.transcribe(
                registry=_FakeRegistry("voice", _Cfg("whisper-1"), client),
                alias="voice",
                data=b"x",
                filename="a.wav",
                cancel_ref=ref,
            )

        manager.__enter__.assert_not_called()
        response.parse.assert_not_called()

    def test_abort_after_omni_manager_creation_prevents_dispatch(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        client = MagicMock()
        ref = StreamAbortRef()
        manager, response = _response_manager(
            parsed=MagicMock(choices=[MagicMock(message=MagicMock(content="too late"))])
        )

        def _create_manager(**_kwargs):
            ref.abort()
            return manager

        client.chat.completions.with_streaming_response.create.side_effect = _create_manager

        with pytest.raises(DeadlineCancelledError):
            audio.transcribe(
                registry=_FakeRegistry(
                    "omni", _Cfg("gemma-omni", {"supports_audio_input": True}), client
                ),
                alias="omni",
                data=b"x",
                filename="a.webm",
                cancel_ref=ref,
            )

        manager.__enter__.assert_not_called()
        response.parse.assert_not_called()


# What ``synthesize`` clones the lane's client with: one attempt, bounded.
_ONE_BOUNDED_ATTEMPT = {
    "max_retries": 0,
    "timeout": httpx2.Timeout(audio._TTS_BACKEND_TIMEOUT_S, connect=5.0),
}


def _tts_client() -> tuple[MagicMock, MagicMock]:
    """An SDK client and the clone ``synthesize`` must send its request through.

    A real clone is its own client with its own ``audio`` resource, so a request
    sent on the original would ignore the clone's options.
    """
    client = MagicMock()
    return client, client.with_options.return_value


class TestSynthesize:
    def test_sends_one_bounded_attempt_through_the_clone(self):
        client, clone = _tts_client()
        manager, _response = _response_manager(body=b"a")
        clone.audio.speech.with_streaming_response.create.return_value = manager
        audio.synthesize(
            registry=_FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client),
            alias="voice",
            text="hi",
            voice="nova",
        )
        client.with_options.assert_called_once_with(**_ONE_BOUNDED_ATTEMPT)
        clone.audio.speech.with_streaming_response.create.assert_called_once()
        client.audio.speech.with_streaming_response.create.assert_not_called()

    def test_calls_audio_speech_and_returns_bytes(self):
        client, clone = _tts_client()
        manager, _response = _response_manager(body=b"RIFF...wavbytes")
        clone.audio.speech.with_streaming_response.create.return_value = manager
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client)
        res = audio.synthesize(registry=reg, alias="voice", text="hi", voice="nova")
        assert res.audio_bytes == b"RIFF...wavbytes"
        assert res.media_type == "audio/mpeg"
        assert res.model_alias == "voice"
        kwargs = clone.audio.speech.with_streaming_response.create.call_args.kwargs
        assert kwargs["voice"] == "nova"
        assert kwargs["input"] == "hi"

    def test_default_voice_when_empty(self):
        client, clone = _tts_client()
        manager, _response = _response_manager(body=b"a")
        clone.audio.speech.with_streaming_response.create.return_value = manager
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client)
        audio.synthesize(registry=reg, alias="voice", text="hi", voice="")
        kwargs = clone.audio.speech.with_streaming_response.create.call_args.kwargs
        assert kwargs["voice"] == "alloy"

    def test_backend_failure_raises_backend_error(self):
        client, clone = _tts_client()
        clone.audio.speech.with_streaming_response.create.side_effect = RuntimeError("down")
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client)
        with pytest.raises(audio.AudioBackendError):
            audio.synthesize(registry=reg, alias="voice", text="hi", voice="nova")

    def test_uses_authenticated_clone_and_pinned_config(self):
        base_client = MagicMock()
        call_client = MagicMock()
        base_client.with_options.return_value = call_client
        bounded = call_client.with_options.return_value
        manager, _response = _response_manager(body=b"voice")
        bounded.audio.speech.with_streaming_response.create.return_value = manager
        cfg = _Cfg("gpt-4o-mini-tts")
        resolver = MagicMock(return_value="minted-token")

        result = audio.synthesize(
            registry=_FakeRegistry("voice", cfg, base_client),
            alias="voice",
            text="hello",
            voice="alloy",
            backend_auth_resolver=resolver,
        )

        assert result.audio_bytes == b"voice"
        resolver.assert_called_once_with("voice", cfg)
        base_client.with_options.assert_called_once_with(api_key="minted-token")
        call_client.with_options.assert_called_once_with(**_ONE_BOUNDED_ATTEMPT)
        base_client.audio.speech.with_streaming_response.create.assert_not_called()
        call_client.audio.speech.with_streaming_response.create.assert_not_called()
        base_client.close.assert_not_called()
        call_client.close.assert_not_called()

    def test_abort_after_speech_manager_creation_prevents_dispatch(self):
        client, clone = _tts_client()
        ref = StreamAbortRef()
        manager, response = _response_manager(body=b"too late")

        def _create_manager(**_kwargs):
            ref.abort()
            return manager

        clone.audio.speech.with_streaming_response.create.side_effect = _create_manager

        with pytest.raises(DeadlineCancelledError):
            audio.synthesize(
                registry=_FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client),
                alias="voice",
                text="hello",
                voice="alloy",
                cancel_ref=ref,
            )

        manager.__enter__.assert_not_called()
        response.read.assert_not_called()


class TestSynthesizeOnTheWire:
    """``synthesize`` through the real SDK client, against a mock backend, so the
    deadline and retry count are the ones the transport actually sees."""

    def test_a_failed_call_is_not_retried_and_carries_the_bound(self):
        seen: list[dict[str, float]] = []

        def backend(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.extensions["timeout"])
            return httpx2.Response(503, json={"error": "overloaded"})

        client = openai.OpenAI(
            api_key="sk-test",
            base_url="http://speech.example.com/v1",
            http_client=httpx2.Client(transport=httpx2.MockTransport(backend)),
        )
        with pytest.raises(audio.AudioBackendError):
            audio.synthesize(
                registry=_FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client),
                alias="voice",
                text="hi",
                voice="nova",
            )
        bound = audio._TTS_BACKEND_TIMEOUT_S
        assert seen == [{"connect": 5.0, "read": bound, "write": bound, "pool": bound}]


class TestOpenAIAudioModelsKnown:
    """The current OpenAI STT/TTS lineup is registered in the static capability
    table, so the admin 'suggested capabilities' recognizes them and they show
    in the known-models list. (Role gating also works via name inference for
    openai-compatible/local backends that aren't in the static table.)"""

    def test_stt_models_flagged(self):
        from turnstone.core.providers import lookup_model_capabilities

        for m in (
            "whisper-1",
            "gpt-4o-transcribe",
            "gpt-4o-mini-transcribe",
            "gpt-4o-transcribe-diarize",  # prefix variant
        ):
            caps = lookup_model_capabilities("openai", m) or {}
            assert caps.get("supports_transcription") is True, m
            assert caps.get("supports_speech_synthesis") is False, m

    def test_tts_models_flagged(self):
        from turnstone.core.providers import lookup_model_capabilities

        for m in ("tts-1", "tts-1-hd", "gpt-4o-mini-tts"):  # tts-1-hd is a prefix variant
            caps = lookup_model_capabilities("openai", m) or {}
            assert caps.get("supports_speech_synthesis") is True, m
            assert caps.get("supports_transcription") is False, m

    def test_chat_model_has_no_audio_flags(self):
        from turnstone.core.providers import lookup_model_capabilities

        caps = lookup_model_capabilities("openai", "gpt-5") or {}
        assert not caps.get("supports_transcription")
        assert not caps.get("supports_speech_synthesis")


class TestTranscribeCached:
    """Memoized STT for the no-native-audio wire fallback."""

    def _result(self, text: str):
        return audio.TranscriptionResult(transcript=text, model_alias="w", model="m")

    def test_memoizes_by_alias_and_hash(self, monkeypatch):
        audio._clear_transcript_cache_for_test()
        calls = []
        reg = _FakeRegistry("w", _Cfg("whisper-1"), MagicMock())

        def fake(binding, **kwargs):
            calls.append(1)
            return self._result("hello world")

        monkeypatch.setattr(audio, "_transcribe_binding", fake)
        kw = dict(registry=reg, alias="w", content_hash="h1", data=b"x", filename="a.wav")
        assert audio.transcribe_cached(**kw) == "hello world"
        assert audio.transcribe_cached(**kw) == "hello world"
        assert len(calls) == 1  # second served from cache

    def test_backend_failure_returns_empty_and_is_not_cached(self, monkeypatch):
        audio._clear_transcript_cache_for_test()
        calls = []
        reg = _FakeRegistry("w", _Cfg("whisper-1"), MagicMock())

        def boom(binding, **kwargs):
            calls.append(1)
            raise audio.AudioBackendError("down")

        monkeypatch.setattr(audio, "_transcribe_binding", boom)
        kw = dict(registry=reg, alias="w", content_hash="h2", data=b"x", filename="a.wav")
        assert audio.transcribe_cached(**kw) == ""
        audio.transcribe_cached(**kw)
        assert len(calls) == 2  # failure not cached -> retried

    @pytest.mark.parametrize("failure_seam", ["resolve", "transcribe"])
    def test_abort_during_backend_failure_propagates_cancellation(
        self,
        monkeypatch,
        failure_seam,
    ):
        audio._clear_transcript_cache_for_test()
        ref = StreamAbortRef()
        reg = _FakeRegistry("w", _Cfg("whisper-1"), MagicMock())

        if failure_seam == "resolve":

            def fail_resolve(**_kwargs):
                ref.abort()
                raise audio.AudioUnavailableError("gone")

            monkeypatch.setattr(audio, "_resolve_audio_binding", fail_resolve)
        else:

            def fail_transcribe(_binding, **_kwargs):
                ref.abort()
                raise audio.AudioBackendError("down")

            monkeypatch.setattr(audio, "_transcribe_binding", fail_transcribe)

        with pytest.raises(DeadlineCancelledError):
            audio.transcribe_cached(
                registry=reg,
                alias="w",
                content_hash="cancelled-failure",
                data=b"x",
                filename="a.wav",
                cancel_ref=ref,
            )

        assert audio._transcript_cache == {}

    def test_disappeared_alias_returns_empty_before_backend_dispatch(self, monkeypatch):
        audio._clear_transcript_cache_for_test()
        transcribe = MagicMock()
        monkeypatch.setattr(audio, "_transcribe_binding", transcribe)
        reg = _FakeRegistry("live", _Cfg("whisper-1"), MagicMock())

        result = audio.transcribe_cached(
            registry=reg,
            alias="removed",
            content_hash="gone",
            data=b"x",
            filename="a.wav",
        )

        assert result == ""
        transcribe.assert_not_called()
        assert audio._transcript_cache == {}

    def test_pre_aborted_unknown_alias_propagates_cancellation(self):
        audio._clear_transcript_cache_for_test()
        ref = StreamAbortRef()
        ref.abort()

        with pytest.raises(DeadlineCancelledError):
            audio.transcribe_cached(
                registry=_FakeRegistry("live", _Cfg("whisper-1"), MagicMock()),
                alias="removed",
                content_hash="gone",
                data=b"x",
                filename="a.wav",
                cancel_ref=ref,
            )

        assert audio._transcript_cache == {}

    def test_cache_isolated_by_principal_and_registry_generation(self, monkeypatch):
        audio._clear_transcript_cache_for_test()
        calls = []
        reg = _FakeRegistry("w", _Cfg("whisper-1"), MagicMock())

        def fake(binding, **kwargs):
            calls.append((binding.registry_generation, kwargs["data"]))
            return self._result(f"result-{len(calls)}")

        monkeypatch.setattr(audio, "_transcribe_binding", fake)
        common = dict(
            registry=reg,
            alias="w",
            content_hash="same",
            data=b"x",
            filename="a.wav",
        )

        assert audio.transcribe_cached(**common, principal_id="user-a") == "result-1"
        assert audio.transcribe_cached(**common, principal_id="user-b") == "result-2"
        assert audio.transcribe_cached(**common, principal_id="user-a") == "result-1"
        reg.generation = 1
        assert audio.transcribe_cached(**common, principal_id="user-a") == "result-3"
        assert calls == [(0, b"x"), (0, b"x"), (1, b"x")]

    def test_racing_empty_result_never_clobbers_real_transcript(self, monkeypatch):
        audio._clear_transcript_cache_for_test()
        reg = _FakeRegistry("w", _Cfg("whisper-1"), MagicMock())

        def racing_empty(binding, **kwargs):
            key = (
                "user-a",
                binding.lane.alias,
                binding.registry_generation,
                "race",
            )
            with audio._transcript_lock:
                audio._transcript_cache[key] = "real from racer"
            return self._result("")

        monkeypatch.setattr(audio, "_transcribe_binding", racing_empty)
        result = audio.transcribe_cached(
            registry=reg,
            alias="w",
            content_hash="race",
            data=b"x",
            filename="a.wav",
            principal_id="user-a",
        )

        assert result == "real from racer"
        assert audio._transcript_cache[("user-a", "w", 0, "race")] == "real from racer"

    def test_pre_aborted_cache_hit_propagates_cancellation(self, monkeypatch):
        audio._clear_transcript_cache_for_test()
        reg = _FakeRegistry("w", _Cfg("whisper-1"), MagicMock())
        monkeypatch.setattr(
            audio,
            "_transcribe_binding",
            lambda binding, **kwargs: self._result("cached"),
        )
        common = dict(
            registry=reg,
            alias="w",
            content_hash="same",
            data=b"x",
            filename="a.wav",
            principal_id="user-a",
        )
        assert audio.transcribe_cached(**common) == "cached"
        ref = StreamAbortRef()
        ref.abort()
        with pytest.raises(DeadlineCancelledError):
            audio.transcribe_cached(**common, cancel_ref=ref)

    def test_backend_auth_refusal_is_not_swallowed(self):
        audio._clear_transcript_cache_for_test()

        def refuse(alias, cfg):
            raise BackendAuthUnavailableError("unavailable")

        with pytest.raises(BackendAuthUnavailableError):
            audio.transcribe_cached(
                registry=_FakeRegistry("w", _Cfg("whisper-1"), MagicMock()),
                alias="w",
                content_hash="h",
                data=b"x",
                filename="a.wav",
                principal_id="user-a",
                backend_auth_resolver=refuse,
            )


# ---------------------------------------------------------------------------
# Omni chat request shaping — transcode + thinking-off + token cap
# ---------------------------------------------------------------------------


class TestOmniChatExtraBody:
    """``_omni_chat_extra_body`` re-applies what the raw-client STT path skips."""

    _THINKING = {"thinking_mode": "manual", "thinking_param": "enable_thinking"}

    def test_disables_thinking_via_model_param(self):
        cfg = _Cfg("gemma", dict(self._THINKING))
        assert audio._omni_chat_extra_body(cfg) == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    def test_thinking_off_wins_over_operator_flag(self):
        cfg = _Cfg(
            "gemma",
            dict(self._THINKING),
            server_compat={"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}},
        )
        # STT never wants reasoning, even if an operator stored thinking on.
        assert audio._omni_chat_extra_body(cfg)["chat_template_kwargs"]["enable_thinking"] is False

    def test_forwards_operator_server_compat_extra_body(self):
        cfg = _Cfg(
            "model",
            dict(self._THINKING),
            server_compat={"extra_body": {"reasoning_format": "auto"}},
        )
        extra = audio._omni_chat_extra_body(cfg)
        assert extra["reasoning_format"] == "auto"
        assert extra["chat_template_kwargs"] == {"enable_thinking": False}

    def test_empty_for_non_thinking_model(self):
        cfg = _Cfg("omni", {"supports_audio_input": True})
        assert audio._omni_chat_extra_body(cfg) == {}


class TestOmniChatCall:
    """The omni chat call carries the thinking-off extra_body and a token cap."""

    def test_sends_thinking_off_and_token_cap(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        client = MagicMock()
        manager, _response = _response_manager(
            parsed=MagicMock(choices=[MagicMock(message=MagicMock(content="hi"))])
        )
        client.chat.completions.with_streaming_response.create.return_value = manager
        cfg = _Cfg(
            "gemma-omni",
            {
                "supports_audio_input": True,
                "thinking_mode": "manual",
                "thinking_param": "enable_thinking",
            },
        )
        audio.transcribe(
            registry=_FakeRegistry("omni", cfg, client),
            alias="omni",
            data=b"webmbytes",
            filename="speech.webm",
        )
        kwargs = client.chat.completions.with_streaming_response.create.call_args.kwargs
        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
        assert kwargs["max_tokens"] == audio._OMNI_STT_MAX_TOKENS


class TestTranscode:
    """``_to_wav_16k_mono`` normalizes any container to 16 kHz mono WAV via ffmpeg."""

    def _stereo_wav_44k(self) -> bytes:
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x01\x00\x01" * 4410)  # 0.1 s of stereo
        return buf.getvalue()

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_transcodes_to_16k_mono(self):
        import io
        import wave

        out = audio._to_wav_16k_mono(self._stereo_wav_44k())
        with wave.open(io.BytesIO(out), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getframerate() == 16000

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_undecodable_bytes_raise_backend_error(self):
        with pytest.raises(audio.AudioBackendError):
            audio._to_wav_16k_mono(b"this is not audio at all")

    def test_missing_ffmpeg_raises_backend_error(self, monkeypatch):
        def _no_ffmpeg(*a, **k):
            raise FileNotFoundError("ffmpeg")

        monkeypatch.setattr(audio.subprocess, "run", _no_ffmpeg)
        with pytest.raises(audio.AudioBackendError, match="ffmpeg is not installed"):
            audio._to_wav_16k_mono(b"x")

    def test_invokes_ffmpeg_with_hardened_argv(self, monkeypatch):
        # Covers the argv shaping even on a CI image without ffmpeg installed.
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            return MagicMock(returncode=0, stdout=b"RIFF....WAVE", stderr=b"")

        monkeypatch.setattr(audio.subprocess, "run", _fake_run)
        assert audio._to_wav_16k_mono(b"rawclip") == b"RIFF....WAVE"
        cmd = captured["cmd"]
        assert cmd[0] == "ffmpeg"
        assert captured["input"] == b"rawclip"
        # SSRF/decompression-bomb hardening + the 16 kHz mono normalization.
        assert cmd[cmd.index("-protocol_whitelist") + 1] == "pipe"
        assert "-vn" in cmd
        assert cmd[cmd.index("-ac") + 1] == "1"
        assert cmd[cmd.index("-ar") + 1] == "16000"
        assert cmd[cmd.index("-f") + 1] == "wav"

    def test_nonzero_returncode_raises_backend_error(self, monkeypatch):
        monkeypatch.setattr(
            audio.subprocess,
            "run",
            lambda *a, **k: MagicMock(returncode=1, stdout=b"", stderr=b"boom"),
        )
        with pytest.raises(audio.AudioBackendError, match="Audio transcode failed"):
            audio._to_wav_16k_mono(b"x")


def _stream_chunk(content):
    return MagicMock(choices=[MagicMock(delta=MagicMock(content=content))])


class TestTranscribeStream:
    """``transcribe_stream`` yields content deltas; resolve/transcode are eager."""

    def test_streams_chat_deltas_with_thinking_off(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        client = MagicMock()
        client.chat.completions.create.return_value = iter(
            [_stream_chunk("and so"), _stream_chunk(None), _stream_chunk(" my fellow americans")]
        )
        cfg = _Cfg(
            "gemma-omni",
            {
                "supports_audio_input": True,
                "thinking_mode": "manual",
                "thinking_param": "enable_thinking",
            },
        )
        gen = audio.transcribe_stream(
            registry=_FakeRegistry("omni", cfg, client), alias="omni", data=b"webmbytes"
        )
        # Empty/None deltas are skipped; the rest stream through in order.
        assert list(gen) == ["and so", " my fellow americans"]
        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["stream"] is True
        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False

    def test_non_audio_provider_raises_before_streaming(self):
        client = MagicMock()
        cfg = _Cfg("gemma", {"supports_audio_input": True}, provider="anthropic-compatible")
        with pytest.raises(audio.AudioUnavailableError, match="OpenAI-compatible provider"):
            audio.transcribe_stream(
                registry=_FakeRegistry("omni", cfg, client), alias="omni", data=b"x"
            )
        client.chat.completions.create.assert_not_called()

    def test_whisper_alias_emits_single_chunk(self):
        client = MagicMock()
        manager, _response = _response_manager(parsed=MagicMock(text="  full transcript  "))
        client.audio.transcriptions.with_streaming_response.create.return_value = manager
        cfg = _Cfg("whisper-1")  # name inference -> dedicated endpoint, no chat stream
        registry = _FakeRegistry("w", cfg, client)
        gen = audio.transcribe_stream(registry=registry, alias="w", data=b"x")
        assert list(gen) == ["full transcript"]
        assert registry.resolve_binding_calls == 1
        client.chat.completions.create.assert_not_called()

    def test_omni_stream_uses_authenticated_clone_and_abort_closes_handle(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        base_client = MagicMock()
        call_client = MagicMock()
        base_client.with_options.return_value = call_client
        stream = MagicMock()
        stream.__iter__.return_value = iter([_stream_chunk("hello")])
        call_client.chat.completions.create.return_value = stream
        cfg = _Cfg("omni", {"supports_audio_input": True})
        resolver = MagicMock(return_value="minted-token")
        ref = StreamAbortRef()

        deltas = audio.transcribe_stream(
            registry=_FakeRegistry("omni", cfg, base_client),
            alias="omni",
            data=b"x",
            backend_auth_resolver=resolver,
            cancel_ref=ref,
        )

        resolver.assert_called_once_with("omni", cfg)
        base_client.with_options.assert_called_once_with(api_key="minted-token")
        base_client.chat.completions.create.assert_not_called()
        ref.abort()
        with pytest.raises(DeadlineCancelledError):
            list(deltas)
        stream.close.assert_called()
        base_client.close.assert_not_called()
        call_client.close.assert_not_called()

    def test_abort_during_final_omni_request_shaping_prevents_dispatch(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        client = MagicMock()
        ref = StreamAbortRef()

        def _abort_in_final_shaping(_cfg):
            ref.abort()
            return {}

        monkeypatch.setattr(audio, "_omni_chat_extra_body", _abort_in_final_shaping)

        with pytest.raises(DeadlineCancelledError):
            audio.transcribe_stream(
                registry=_FakeRegistry(
                    "omni", _Cfg("gemma-omni", {"supports_audio_input": True}), client
                ),
                alias="omni",
                data=b"x",
                cancel_ref=ref,
            )

        client.chat.completions.create.assert_not_called()


# ---------------------------------------------------------------------------
# The alias's max_concurrency gate
# ---------------------------------------------------------------------------


class _ClockAnHourAhead:
    """Stands in for ``time`` in turnstone.core.admission: an hour ahead of the real clock."""

    @staticmethod
    def monotonic() -> float:
        return time.monotonic() + 3600.0


def _omni_chat_manager(text: str):
    manager, _response = _response_manager(
        parsed=MagicMock(choices=[MagicMock(message=MagicMock(content=text))])
    )
    return manager


class TestAudioAdmission:
    """Every audio backend call holds one of its alias's ``max_concurrency`` slots."""

    def test_a_transcription_holds_a_slot_for_its_call(self):
        gate = ModelAdmission("voice", 1)
        client = MagicMock()
        manager, _response = _response_manager(parsed=MagicMock(text="hi"))
        held: list[int] = []

        def _create(**_kwargs):
            held.append(gate.snapshot().in_flight)
            return manager

        client.audio.transcriptions.with_streaming_response.create.side_effect = _create
        result = audio.transcribe(
            registry=_FakeRegistry("voice", _Cfg("whisper-1"), client, gate),
            alias="voice",
            data=b"x",
            filename="a.wav",
        )

        assert result.transcript == "hi"
        assert held == [1]
        assert gate.snapshot().in_flight == 0

    def test_an_omni_transcription_takes_its_slot_after_the_transcode(self, monkeypatch):
        # The transcode is local work and can take seconds, so it must not hold
        # the backend's slot.
        gate = ModelAdmission("omni", 1)
        held: dict[str, int] = {}

        def _transcode(data: bytes, **kwargs) -> bytes:
            held["transcode"] = gate.snapshot().in_flight
            return data

        monkeypatch.setattr(audio, "_to_wav_16k_mono", _transcode)
        client = MagicMock()
        manager = _omni_chat_manager("hello")

        def _create(**_kwargs):
            held["create"] = gate.snapshot().in_flight
            return manager

        client.chat.completions.with_streaming_response.create.side_effect = _create
        result = audio.transcribe(
            registry=_FakeRegistry(
                "omni", _Cfg("gemma-omni", {"supports_audio_input": True}), client, gate
            ),
            alias="omni",
            data=b"x",
            filename="a.webm",
        )

        assert result.transcript == "hello"
        assert held == {"transcode": 0, "create": 1}
        assert gate.snapshot().in_flight == 0

    def test_a_full_alias_refuses_a_transcription_once_its_wait_runs_out(self):
        gate = ModelAdmission("voice", 1)
        client = MagicMock()

        with slot_held(gate):
            started = time.monotonic()
            with pytest.raises(audio.AudioBusyError):
                audio.transcribe(
                    registry=_FakeRegistry("voice", _Cfg("whisper-1"), client, gate),
                    alias="voice",
                    data=b"x",
                    filename="a.wav",
                    admission_wait_s=0.05,
                )
            assert time.monotonic() - started >= 0.05

        client.audio.transcriptions.with_streaming_response.create.assert_not_called()
        assert gate.snapshot().queued == 0

    def test_an_in_turn_transcription_waits_until_its_turn_is_cancelled(self, monkeypatch):
        """The wire fallback waits for a slot as a generation does: with no
        bound, until the turn's cancel ref aborts."""
        audio._clear_transcript_cache_for_test()
        gate = ModelAdmission("w", 1)
        holder = gate.acquire()
        client = MagicMock()
        ref = StreamAbortRef()
        errors: list[BaseException] = []
        finished = threading.Event()

        def _transcribe() -> None:
            try:
                audio.transcribe_cached(
                    registry=_FakeRegistry("w", _Cfg("whisper-1"), client, gate),
                    alias="w",
                    content_hash="queued",
                    data=b"x",
                    filename="a.wav",
                    cancel_ref=ref,
                )
            except BaseException as exc:  # test records the worker's exact exit
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=_transcribe, daemon=True)
        thread.start()
        wait_until(lambda: gate.snapshot().queued == 1)
        # An hour passes on the gate's clock, so any bound on the wait would run out.
        monkeypatch.setattr(admission_mod, "time", _ClockAnHourAhead)
        assert not finished.wait(0.6)
        ref.abort()

        assert finished.wait(2.0)
        assert len(errors) == 1
        assert isinstance(errors[0], DeadlineCancelledError)
        assert gate.snapshot().queued == 0
        client.audio.transcriptions.with_streaming_response.create.assert_not_called()
        holder.release()

    def test_an_open_stream_holds_its_slot_until_it_has_been_read(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("omni", 1)
        stream = MagicMock()
        stream.__iter__.return_value = iter([_stream_chunk("hello")])
        client = MagicMock()
        client.chat.completions.create.return_value = stream

        deltas = audio.transcribe_stream(
            registry=_FakeRegistry(
                "omni", _Cfg("omni", {"supports_audio_input": True}), client, gate
            ),
            alias="omni",
            data=b"x",
            cancel_ref=StreamAbortRef(),
        )

        assert gate.snapshot().in_flight == 1
        assert list(deltas) == ["hello"]
        assert gate.snapshot().in_flight == 0
        stream.close.assert_called_once()

    def test_closing_a_stream_nobody_read_gives_its_slot_back(self, monkeypatch):
        # No cancel ref: closing the returned iterator alone must free the slot,
        # which a generator closed before its first next() would not.
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("omni", 1)
        stream = MagicMock()
        client = MagicMock()
        client.chat.completions.create.return_value = stream

        deltas = audio.transcribe_stream(
            registry=_FakeRegistry(
                "omni", _Cfg("omni", {"supports_audio_input": True}), client, gate
            ),
            alias="omni",
            data=b"x",
        )
        assert gate.snapshot().in_flight == 1
        deltas.close()

        assert gate.snapshot().in_flight == 0
        stream.close.assert_called_once()

    def test_aborting_a_stream_nobody_read_gives_its_slot_back(self, monkeypatch):
        # The endpoint aborts its ref on every exit, so an iterator it drops
        # unread and unclosed still frees the slot.
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("omni", 1)
        stream = MagicMock()
        client = MagicMock()
        client.chat.completions.create.return_value = stream
        ref = StreamAbortRef()

        deltas = audio.transcribe_stream(
            registry=_FakeRegistry(
                "omni", _Cfg("omni", {"supports_audio_input": True}), client, gate
            ),
            alias="omni",
            data=b"x",
            cancel_ref=ref,
        )
        ref.abort()
        assert gate.snapshot().in_flight == 0
        stream.close.assert_called_once()
        # A second closer frees nothing more.
        deltas.close()
        assert gate.snapshot().in_flight == 0
        stream.close.assert_called_once()

    def test_a_stream_that_fails_to_open_gives_its_slot_back(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("omni", 1)
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("backend down")

        with pytest.raises(audio.AudioBackendError):
            audio.transcribe_stream(
                registry=_FakeRegistry(
                    "omni", _Cfg("omni", {"supports_audio_input": True}), client, gate
                ),
                alias="omni",
                data=b"x",
            )

        assert gate.snapshot().in_flight == 0

    def test_a_stream_aborted_as_it_opens_gives_its_slot_back(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("omni", 1)
        stream = MagicMock()
        client = MagicMock()
        ref = StreamAbortRef()

        def _create(**_kwargs):
            ref.abort()  # the caller leaves while the backend answers
            return stream

        client.chat.completions.create.side_effect = _create

        with pytest.raises(DeadlineCancelledError):
            audio.transcribe_stream(
                registry=_FakeRegistry(
                    "omni", _Cfg("omni", {"supports_audio_input": True}), client, gate
                ),
                alias="omni",
                data=b"x",
                cancel_ref=ref,
            )

        assert gate.snapshot().in_flight == 0
        stream.close.assert_called_once()

    def test_a_full_alias_refuses_a_stream_before_it_opens(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("omni", 1)
        client = MagicMock()

        with slot_held(gate), pytest.raises(audio.AudioBusyError):
            audio.transcribe_stream(
                registry=_FakeRegistry(
                    "omni", _Cfg("omni", {"supports_audio_input": True}), client, gate
                ),
                alias="omni",
                data=b"x",
                admission_wait_s=0,
            )

        client.chat.completions.create.assert_not_called()
        assert gate.snapshot().queued == 0

    def test_a_synthesis_holds_a_slot_for_its_call(self):
        gate = ModelAdmission("voice", 1)
        client, clone = _tts_client()
        manager, _response = _response_manager(body=b"audio")
        held: list[int] = []

        def _create(**_kwargs):
            held.append(gate.snapshot().in_flight)
            return manager

        clone.audio.speech.with_streaming_response.create.side_effect = _create
        result = audio.synthesize(
            registry=_FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client, gate),
            alias="voice",
            text="hi",
            voice="nova",
            admission_wait_s=0,
        )

        assert result.audio_bytes == b"audio"
        assert held == [1]
        assert gate.snapshot().in_flight == 0

    def test_a_full_alias_refuses_a_synthesis_at_once(self):
        gate = ModelAdmission("voice", 1)
        client, clone = _tts_client()

        with slot_held(gate), pytest.raises(audio.AudioBusyError):
            audio.synthesize(
                registry=_FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client, gate),
                alias="voice",
                text="hi",
                voice="nova",
                admission_wait_s=0,
            )

        clone.audio.speech.with_streaming_response.create.assert_not_called()
        assert gate.snapshot().queued == 0


# ---------------------------------------------------------------------------
# HTTP dictation budgets through the real SDK
# ---------------------------------------------------------------------------


def _dictate_on_lane(case, client, gate, **kwargs):
    cfg = (
        _Cfg("whisper-1")
        if case.startswith("endpoint")
        else _Cfg("omni", {"supports_audio_input": True})
    )
    arguments = dict(
        registry=_FakeRegistry("voice", cfg, client, gate),
        alias="voice",
        data=b"audio",
    )
    arguments.update(kwargs)
    if case.endswith("stream"):
        return list(audio.transcribe_stream(**arguments))
    return audio.transcribe(filename="speech.webm", **arguments)


@contextlib.contextmanager
def _stalled_transcription_backend(
    case, *, trickle=False, send_stream_headers=False, stream_delta=None, stall_first=True
):
    """Stall the first request during upload, before headers, or before stream data."""
    state = SimpleNamespace(
        requests=0,
        uploaded_bytes=0,
        request_bytes=0,
        upload_finished_at=None,
        stop=threading.Event(),
        start_body=threading.Event(),
        workers=[],
    )
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.close_connection = True
            with lock:
                state.requests += 1
                request_number = state.requests
                state.workers.append(threading.current_thread())
            self.connection.settimeout(3.0)
            remaining = int(self.headers["Content-Length"])
            if request_number == 1 and stall_first:
                state.request_bytes = remaining
                if not trickle and not send_stream_headers and state.stop.wait(0.6):
                    return
                try:
                    while remaining and not state.stop.is_set():
                        chunk = self.rfile.read(min(8192, remaining) if trickle else remaining)
                        if not chunk:
                            return
                        state.uploaded_bytes += len(chunk)
                        remaining -= len(chunk)
                        if trickle and state.stop.wait(0.05):
                            return
                except OSError:
                    # A request deadline may shut down a partially sent upload.
                    return
                if remaining:
                    return
                state.upload_finished_at = time.monotonic()
                if send_stream_headers:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.flush()
                    if stream_delta is not None and state.start_body.wait(3.0):
                        payload = json.dumps(
                            {
                                "id": "transcript",
                                "object": "chat.completion.chunk",
                                "created": 0,
                                "model": "omni",
                                "choices": [{"index": 0, "delta": {"content": stream_delta}}],
                            }
                        ).encode()
                        try:
                            self.wfile.write(b"data: " + payload + b"\n\n")
                            self.wfile.flush()
                        except OSError:
                            return
                state.stop.wait(5.0)
                return

            self.rfile.read(remaining)
            if case.startswith("endpoint"):
                payload = json.dumps({"text": "recovered"}).encode()
                content_type = "application/json"
            else:
                streaming = case.endswith("stream")
                result = {
                    "id": "transcript",
                    "object": "chat.completion.chunk" if streaming else "chat.completion",
                    "created": 0,
                    "model": "omni",
                    "choices": [
                        {
                            "index": 0,
                            "delta" if streaming else "message": {
                                "role": "assistant",
                                "content": "recovered",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
                payload = json.dumps(result).encode()
                if streaming:
                    payload = b"data: " + payload + b"\n\ndata: [DONE]\n\n"
                content_type = "text/event-stream" if streaming else "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    class Server(ThreadingHTTPServer):
        daemon_threads = False

    server = Server(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    server_thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        state.stop.set()
        state.start_body.set()
        server.shutdown()
        server.server_close()  # Joins every non-daemon request worker.
        server_thread.join(timeout=3.0)
        assert not server_thread.is_alive()
        assert all(not worker.is_alive() for worker in state.workers)


@pytest.mark.parametrize("case", ["endpoint", "endpoint-stream", "omni", "omni-stream"])
class TestTranscriptionHttpBudget:
    def test_upload_and_silent_headers_share_one_deadline(self, monkeypatch, case):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("voice", 1)
        budget = 1.2
        transport = httpx2.HTTPTransport(
            socket_options=[(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)]
        )
        with (
            _stalled_transcription_backend(case) as backend,
            openai.OpenAI(
                api_key="sk-test",
                base_url=backend.base_url,
                max_retries=2,
                timeout=3.0,
                http_client=shutdown_on_close(httpx2.Client(transport=transport)),
            ) as client,
        ):
            started = time.monotonic()
            with pytest.raises(audio.AudioBackendError) as caught:
                _dictate_on_lane(
                    case,
                    client,
                    gate,
                    data=b"x" * (1024 * 1024),
                    deadline=started + budget,
                )
            elapsed = time.monotonic() - started
            causes = []
            cause = caught.value
            while cause is not None:
                causes.append(cause)
                cause = cause.__cause__
            assert any(isinstance(cause, openai.APITimeoutError) for cause in causes)
            assert any(isinstance(cause, httpx2.ReadTimeout) for cause in causes)
            assert backend.requests == 1
            assert backend.upload_finished_at is not None
            assert 0.55 <= backend.upload_finished_at - started < budget
            assert backend.uploaded_bytes == backend.request_bytes
            assert gate.snapshot().in_flight == 0

            # The expired request must not leave its deadline attached to this client.
            recovered = _dictate_on_lane(case, client, gate)
            assert (
                "".join(recovered) if case.endswith("stream") else recovered.transcript
            ) == "recovered"
            assert backend.requests == 2
            assert gate.snapshot().in_flight == 0
            assert client.max_retries == 2
            assert client.timeout == 3.0
            assert elapsed <= budget + 0.3, f"upload plus silent read took {elapsed:.3f}s"

    @pytest.mark.parametrize("failure", ["retry-after", "timeout"])
    def test_failed_attempt_is_not_retried_and_releases_the_slot(self, monkeypatch, case, failure):
        monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: 100.0))
        monkeypatch.setattr(provider_http, "time", audio.time)
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("voice", 1)
        seen = []

        def backend(request):
            seen.append(request.extensions["timeout"])
            assert gate.snapshot().in_flight == 1
            assert request.headers["authorization"] == "Bearer minted-token"
            if failure == "timeout":
                raise httpx2.ReadTimeout("backend silent before headers", request=request)
            return httpx2.Response(
                503, headers={"Retry-After": "120"}, json={"error": "overloaded"}
            )

        with openai.OpenAI(
            api_key="original-token",
            base_url="http://speech.example.com/v1",
            max_retries=2,
            timeout=600.0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(backend)),
        ) as client:
            with pytest.raises(audio.AudioBackendError):
                _dictate_on_lane(
                    case,
                    client,
                    gate,
                    deadline=103.0,
                    backend_auth_resolver=lambda *_: "minted-token",
                )
            assert client.api_key == "original-token"
            assert client.max_retries == 2
            assert client.timeout == 600.0

        assert seen == [{"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}]
        assert gate.snapshot().in_flight == 0

    def test_no_http_deadline_preserves_client_timeouts_and_retries(self, monkeypatch, case):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("voice", 1)
        seen = []

        def backend(request):
            seen.append(request.extensions["timeout"])
            return httpx2.Response(
                503, headers={"Retry-After": "0.001"}, json={"error": "overloaded"}
            )

        with (
            openai.OpenAI(
                api_key="sk-test",
                base_url="http://speech.example.com/v1",
                max_retries=1,
                timeout=17.0,
                http_client=httpx2.Client(transport=httpx2.MockTransport(backend)),
            ) as client,
            pytest.raises(audio.AudioBackendError),
        ):
            _dictate_on_lane(case, client, gate)

        timeout = audio._OMNI_STT_TIMEOUT_S if case == "omni-stream" else 17.0
        assert seen == [dict.fromkeys(("connect", "read", "write", "pool"), timeout)] * 2
        assert gate.snapshot().in_flight == 0

    def test_preparation_admission_and_auth_share_the_budget(self, monkeypatch, case):
        now = [100.0]
        monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(provider_http, "time", audio.time)
        gate = ModelAdmission("voice", 1)
        original_acquire = gate.acquire
        seen = {}

        def transcode(*args, **kwargs):
            seen["transcode"] = kwargs["timeout"]
            assert gate.snapshot().in_flight == 0
            now[0] += 2.0
            return SimpleNamespace(returncode=0, stdout=b"wav", stderr=b"")

        def acquire(**kwargs):
            seen["admission"] = kwargs["timeout"]
            now[0] += 3.0
            return original_acquire(**kwargs)

        def authenticate(*args):
            assert gate.snapshot().in_flight == 1
            now[0] += 4.0
            return "minted-token"

        def backend(request):
            seen["backend"] = request.extensions["timeout"]
            assert request.headers["authorization"] == "Bearer minted-token"
            return httpx2.Response(400, json={"error": "test complete"})

        monkeypatch.setattr(audio.subprocess, "run", transcode)
        monkeypatch.setattr(gate, "acquire", acquire)
        with (
            openai.OpenAI(
                api_key="original-token",
                base_url="http://speech.example.com/v1",
                http_client=httpx2.Client(transport=httpx2.MockTransport(backend)),
            ) as client,
            pytest.raises(audio.AudioBackendError),
        ):
            _dictate_on_lane(
                case,
                client,
                gate,
                deadline=125.0,
                admission_wait_s=100.0,
                backend_auth_resolver=authenticate,
            )

        if case.startswith("omni"):
            assert seen["transcode"] == 25.0
            assert seen["admission"] == 23.0
            remaining = 16.0
        else:
            assert "transcode" not in seen
            assert seen["admission"] == 25.0
            remaining = 18.0
        assert seen["backend"] == dict(connect=5.0, read=remaining, write=remaining, pool=remaining)
        assert gate.snapshot().in_flight == 0

    def test_expired_auth_budget_prevents_dispatch_and_releases_the_slot(self, monkeypatch, case):
        now = [100.0]
        monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(provider_http, "time", audio.time)
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
        gate = ModelAdmission("voice", 1)
        client = MagicMock()

        def authenticate(*args):
            assert gate.snapshot().in_flight == 1
            now[0] = 126.0
            return "minted-token"

        with pytest.raises(audio.AudioBackendError, match="timed out"):
            _dictate_on_lane(case, client, gate, deadline=125.0, backend_auth_resolver=authenticate)

        client.with_options.assert_called_once_with(api_key="minted-token")
        client.with_options.return_value.with_options.assert_not_called()
        client.with_options.return_value.audio.transcriptions.with_streaming_response.create.assert_not_called()
        client.with_options.return_value.chat.completions.with_streaming_response.create.assert_not_called()
        client.with_options.return_value.chat.completions.create.assert_not_called()
        assert gate.snapshot().in_flight == 0


def test_slow_dns_releases_dictation_capacity_before_resolver_returns(monkeypatch):
    gate = ModelAdmission("voice", 1)
    resolver_started = threading.Event()
    release_resolver = threading.Event()
    resolver_threads = []
    original_getaddrinfo = socket.getaddrinfo

    def slow_getaddrinfo(host, port, *args, **kwargs):
        if host in ("slow.invalid", b"slow.invalid"):
            resolver_threads.append(threading.current_thread())
            assert gate.snapshot().in_flight == 1
            resolver_started.set()
            release_resolver.wait(1.0)
            host = "127.0.0.1"
        return original_getaddrinfo(host, port, *args, **kwargs)

    with (
        _stalled_transcription_backend("endpoint", stall_first=False) as backend,
        openai.OpenAI(
            api_key="sk-test",
            base_url=backend.base_url.replace("127.0.0.1", "slow.invalid"),
            http_client=shutdown_on_close(httpx2.Client(trust_env=False)),
        ) as client,
    ):
        monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
        try:
            started = time.monotonic()
            with pytest.raises(audio.AudioBackendError) as caught:
                _dictate_on_lane("endpoint", client, gate, deadline=started + 0.15)
            elapsed = time.monotonic() - started
            assert elapsed < 0.7, f"slow DNS held dictation capacity for {elapsed:.3f}s"
            assert resolver_started.is_set()
            assert not release_resolver.is_set()
            assert resolver_threads[0].is_alive()
            assert gate.snapshot().in_flight == 0
            assert backend.requests == 0
            causes = []
            cause = caught.value
            while cause is not None:
                causes.append(cause)
                cause = cause.__cause__
            assert any(isinstance(cause, openai.APITimeoutError) for cause in causes)
            assert any(isinstance(cause, httpx2.ConnectTimeout) for cause in causes)
        finally:
            release_resolver.set()
            for thread in resolver_threads:
                thread.join(timeout=3.0)
                assert not thread.is_alive()

        # Completing abandoned DNS must not connect or submit a late request.
        assert backend.requests == 0
        client.base_url = backend.base_url
        assert _dictate_on_lane("endpoint", client, gate).transcript == "recovered"
        assert backend.requests == 1
        assert gate.snapshot().in_flight == 0


def test_trickling_upload_cannot_renew_the_transcription_deadline():
    gate = ModelAdmission("voice", 1)
    budget = 1.0
    transport = httpx2.HTTPTransport(socket_options=[(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)])
    with (
        _stalled_transcription_backend("endpoint", trickle=True) as backend,
        openai.OpenAI(
            api_key="sk-test",
            base_url=backend.base_url,
            max_retries=2,
            timeout=3.0,
            http_client=shutdown_on_close(httpx2.Client(transport=transport)),
        ) as client,
    ):
        started = time.monotonic()
        with pytest.raises(audio.AudioBackendError) as caught:
            _dictate_on_lane(
                "endpoint",
                client,
                gate,
                data=b"x" * (1024 * 1024),
                deadline=started + budget,
            )
        elapsed = time.monotonic() - started
        causes = []
        cause = caught.value
        while cause is not None:
            causes.append(cause)
            cause = cause.__cause__
        assert any(isinstance(cause, openai.APITimeoutError) for cause in causes)
        assert any(isinstance(cause, httpx2.WriteTimeout) for cause in causes)
        assert backend.requests == 1
        assert 0 < backend.uploaded_bytes < backend.request_bytes
        assert backend.upload_finished_at is None
        assert gate.snapshot().in_flight == 0
        assert _dictate_on_lane("endpoint", client, gate).transcript == "recovered"
        assert backend.requests == 2
        assert gate.snapshot().in_flight == 0
        assert elapsed <= budget + 0.3, f"trickling upload took {elapsed:.3f}s"


def test_unread_omni_stream_deadline_closes_response_and_releases_slot(monkeypatch):
    monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
    gate = ModelAdmission("voice", 1)
    responses = []
    budget = 0.8
    with (
        _stalled_transcription_backend("omni-stream", send_stream_headers=True) as backend,
        openai.OpenAI(
            api_key="sk-test",
            base_url=backend.base_url,
            http_client=shutdown_on_close(
                httpx2.Client(event_hooks={"response": [responses.append]})
            ),
        ) as client,
    ):
        started = time.monotonic()
        stream = audio.transcribe_stream(
            registry=_FakeRegistry(
                "voice", _Cfg("omni", {"supports_audio_input": True}), client, gate
            ),
            alias="voice",
            data=b"audio",
            deadline=started + budget,
        )
        try:
            assert gate.snapshot().in_flight == 1
            assert len(responses) == 1
            assert not responses[0].is_closed
            # No next() call: expiry must close and release an idle consumer's stream.
            wait_until(lambda: gate.snapshot().in_flight == 0, timeout=budget + 0.3)
            assert time.monotonic() - started <= budget + 0.3
            assert responses[0].is_closed
            assert backend.requests == 1
            with pytest.raises(audio.AudioBackendError, match="deadline exceeded"):
                next(stream)
            assert _dictate_on_lane("omni-stream", client, gate) == ["recovered"]
            assert gate.snapshot().in_flight == 0
        finally:
            stream.close()


def test_omni_stream_carries_deadline_to_another_worker_and_restores_context(monkeypatch):
    monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
    gate = ModelAdmission("voice", 1)
    budget = 0.8
    network_reads = []
    original_read = provider_http._ShutdownStream.read

    def observe_read(self, *args, **kwargs):
        # Observe the real socket boundary without replacing its blocking read.
        network_reads.append((threading.get_ident(), provider_http._request_deadline.get()))
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(provider_http._ShutdownStream, "read", observe_read)
    with (
        _stalled_transcription_backend(
            "omni-stream", send_stream_headers=True, stream_delta="first"
        ) as backend,
        openai.OpenAI(
            api_key="sk-test",
            base_url=backend.base_url,
            http_client=shutdown_on_close(httpx2.Client()),
        ) as client,
        ThreadPoolExecutor(max_workers=1) as opening_worker,
        ThreadPoolExecutor(max_workers=1) as consuming_worker,
    ):
        started = time.monotonic()
        deadline = started + budget

        def open_stream():
            stream = audio.transcribe_stream(
                registry=_FakeRegistry(
                    "voice", _Cfg("omni", {"supports_audio_input": True}), client, gate
                ),
                alias="voice",
                data=b"audio",
                deadline=deadline,
            )
            assert provider_http._request_deadline.get() is None
            return threading.get_ident(), stream

        opening_thread, stream = opening_worker.submit(open_stream).result(timeout=3.0)

        def consume_stream():
            assert provider_http._request_deadline.get() is None
            backend.start_body.set()
            try:
                assert next(stream) == "first"
                assert time.monotonic() < deadline
                assert gate.snapshot().in_flight == 1
                assert provider_http._request_deadline.get() is None
                # The backend remains silent after the first delta. The request's
                # original deadline must govern this worker's next socket read.
                with pytest.raises(openai.APIConnectionError) as caught:
                    next(stream)
                assert provider_http._request_deadline.get() is None
                return threading.get_ident(), caught.value, time.monotonic() - started
            finally:
                stream.close()

        try:
            consuming_thread, failure, elapsed = consuming_worker.submit(consume_stream).result(
                timeout=3.0
            )
            assert consuming_thread != opening_thread
            consumer_budgets = [
                value for thread, value in network_reads if thread == consuming_thread
            ]
            assert consumer_budgets
            assert all(
                value is not None and value.deadline == deadline for value in consumer_budgets
            )
            causes = []
            while failure is not None:
                causes.append(failure)
                failure = failure.__cause__
            assert any(isinstance(cause, httpx2.ReadTimeout) for cause in causes)
            assert elapsed <= budget + 0.3, f"cross-worker stream read took {elapsed:.3f}s"
            assert gate.snapshot().in_flight == 0
            assert backend.requests == 1
            assert _dictate_on_lane("omni-stream", client, gate) == ["recovered"]
            assert gate.snapshot().in_flight == 0
        finally:
            stream.close()


@pytest.mark.parametrize("ending", ["exhausted", "closed", "failed", "expired"])
def test_a_finished_stream_stays_exhausted_after_its_deadline(monkeypatch, ending):
    now = [100.0]
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(provider_http, "time", audio.time)
    monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data, **kwargs: data)
    gate = ModelAdmission("voice", 1)

    def chunks():
        yield _stream_chunk("hello")
        if ending == "failed":
            raise RuntimeError("backend reset")

    client = MagicMock()
    client.with_options.return_value.chat.completions.create.return_value = chunks()
    deltas = audio.transcribe_stream(
        registry=_FakeRegistry("voice", _Cfg("omni", {"supports_audio_input": True}), client, gate),
        alias="voice",
        data=b"audio",
        deadline=125.0,
    )
    try:
        assert next(deltas) == "hello"
        if ending == "closed":
            deltas.close()
        elif ending == "failed":
            with pytest.raises(RuntimeError, match="backend reset"):
                next(deltas)
        elif ending == "expired":
            # The timer has not fired, so only the failed next() can give the slot back.
            now[0] = 126.0
            with pytest.raises(audio.AudioBackendError, match="deadline exceeded"):
                next(deltas)
        else:
            with pytest.raises(StopIteration):
                next(deltas)
        assert gate.snapshot().in_flight == 0
        now[0] = 126.0
        # A deadline that passes after the stream finished is not its outcome.
        with pytest.raises(StopIteration):
            next(deltas)
    finally:
        deltas.close()


@pytest.mark.parametrize("stream", [False, True])
def test_expired_transcode_budget_does_not_take_a_slot(monkeypatch, stream):
    now = [100.0]
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(provider_http, "time", audio.time)
    gate = ModelAdmission("voice", 1)
    acquire = MagicMock(wraps=gate.acquire)
    monkeypatch.setattr(gate, "acquire", acquire)
    client = MagicMock()

    def transcode(*args, **kwargs):
        assert kwargs["timeout"] == 25.0
        now[0] = 126.0
        return SimpleNamespace(returncode=0, stdout=b"wav", stderr=b"")

    monkeypatch.setattr(audio.subprocess, "run", transcode)
    with pytest.raises(audio.AudioBackendError, match="timed out") as caught:
        _dictate_on_lane("omni-stream" if stream else "omni", client, gate, deadline=125.0)

    # A local budget timeout is not reported as a backend failure on either route.
    assert str(caught.value) == "Speech transcription request timed out"
    acquire.assert_not_called()
    client.with_options.assert_not_called()
    assert gate.snapshot().in_flight == 0
