"""Unit tests for the STT/TTS audio helper (model-role resolution + backends).

``transcribe`` / ``synthesize`` are exercised through the registry boundary
with a mocked OpenAI-SDK client (mocking ``client.audio.*``), so the real
helper code runs end-to-end without a network call.
"""

from __future__ import annotations

import shutil
from unittest.mock import MagicMock

import pytest

from turnstone.core import audio
from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef
from turnstone.core.model_backend_auth import BackendAuthUnavailableError
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
    """Minimal registry exposing the surface audio.py uses."""

    def __init__(self, alias: str, cfg: _Cfg, client: object) -> None:
        self._alias = alias
        self._cfg = cfg
        self._client = client
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
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data: data)
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
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data: data)
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
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data: data)
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
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data: data)
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


class TestSynthesize:
    def test_calls_audio_speech_and_returns_bytes(self):
        client = MagicMock()
        manager, _response = _response_manager(body=b"RIFF...wavbytes")
        client.audio.speech.with_streaming_response.create.return_value = manager
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client)
        res = audio.synthesize(registry=reg, alias="voice", text="hi", voice="nova")
        assert res.audio_bytes == b"RIFF...wavbytes"
        assert res.media_type == "audio/mpeg"
        assert res.model_alias == "voice"
        kwargs = client.audio.speech.with_streaming_response.create.call_args.kwargs
        assert kwargs["voice"] == "nova"
        assert kwargs["input"] == "hi"

    def test_default_voice_when_empty(self):
        client = MagicMock()
        manager, _response = _response_manager(body=b"a")
        client.audio.speech.with_streaming_response.create.return_value = manager
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client)
        audio.synthesize(registry=reg, alias="voice", text="hi", voice="")
        kwargs = client.audio.speech.with_streaming_response.create.call_args.kwargs
        assert kwargs["voice"] == "alloy"

    def test_backend_failure_raises_backend_error(self):
        client = MagicMock()
        client.audio.speech.with_streaming_response.create.side_effect = RuntimeError("down")
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client)
        with pytest.raises(audio.AudioBackendError):
            audio.synthesize(registry=reg, alias="voice", text="hi", voice="nova")

    def test_uses_authenticated_clone_and_pinned_config(self):
        base_client = MagicMock()
        call_client = MagicMock()
        base_client.with_options.return_value = call_client
        manager, _response = _response_manager(body=b"voice")
        call_client.audio.speech.with_streaming_response.create.return_value = manager
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
        base_client.audio.speech.with_streaming_response.create.assert_not_called()
        base_client.close.assert_not_called()
        call_client.close.assert_not_called()

    def test_abort_after_speech_manager_creation_prevents_dispatch(self):
        client = MagicMock()
        ref = StreamAbortRef()
        manager, response = _response_manager(body=b"too late")

        def _create_manager(**_kwargs):
            ref.abort()
            return manager

        client.audio.speech.with_streaming_response.create.side_effect = _create_manager

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
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data: data)
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
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data: data)
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
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data: data)
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
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data: data)
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
