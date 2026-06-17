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

    def has_alias(self, alias: str) -> bool:
        return alias == self._alias

    def get_config(self, alias: str) -> _Cfg:
        if alias != self._alias:
            raise ValueError(alias)
        return self._cfg

    def resolve(self, alias: str | None = None):
        if alias not in (None, self._alias):
            raise ValueError(alias)
        return self._client, self._cfg.model, self._cfg


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
        client.audio.transcriptions.create.return_value = MagicMock(text="  hello world  ")
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-transcribe"), client)
        res = audio.transcribe(
            registry=reg, alias="voice", data=b"RIFFfake", filename="speech.webm"
        )
        assert res.transcript == "hello world"
        assert res.model_alias == "voice"
        assert res.model == "gpt-4o-mini-transcribe"
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        assert kwargs["model"] == "gpt-4o-mini-transcribe"
        assert kwargs["file"] == ("speech.webm", b"RIFFfake")

    def test_prompt_forwarded_when_set(self):
        client = MagicMock()
        client.audio.transcriptions.create.return_value = MagicMock(text="ok")
        reg = _FakeRegistry("voice", _Cfg("whisper-1"), client)
        audio.transcribe(
            registry=reg, alias="voice", data=b"x", filename="a.wav", prompt="ACME jargon"
        )
        assert client.audio.transcriptions.create.call_args.kwargs["prompt"] == "ACME jargon"

    def test_prompt_omitted_when_blank(self):
        client = MagicMock()
        client.audio.transcriptions.create.return_value = MagicMock(text="ok")
        reg = _FakeRegistry("voice", _Cfg("whisper-1"), client)
        audio.transcribe(registry=reg, alias="voice", data=b"x", filename="a.wav")
        assert "prompt" not in client.audio.transcriptions.create.call_args.kwargs

    def test_backend_failure_raises_backend_error(self):
        client = MagicMock()
        client.audio.transcriptions.create.side_effect = RuntimeError("boom")
        reg = _FakeRegistry("voice", _Cfg("whisper-1"), client)
        with pytest.raises(audio.AudioBackendError):
            audio.transcribe(registry=reg, alias="voice", data=b"x", filename="a.wav")

    def test_omni_model_transcribes_via_chat(self, monkeypatch):
        monkeypatch.setattr(audio, "_to_wav_16k_mono", lambda data: data)
        client = MagicMock()
        msg = MagicMock(content="  the transcript  ")
        client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
        reg = _FakeRegistry("omni", _Cfg("gemma-omni", {"supports_audio_input": True}), client)
        res = audio.transcribe(
            registry=reg, alias="omni", data=b"webmbytes", filename="speech.webm"
        )
        assert res.transcript == "the transcript"
        # The dedicated transcription endpoint is NOT used for an omni model.
        client.audio.transcriptions.create.assert_not_called()
        parts = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
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
        client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="x"))]
        )
        reg = _FakeRegistry("omni", _Cfg("gemma-omni", {"supports_audio_input": True}), client)
        audio.transcribe(
            registry=reg, alias="omni", data=b"x", filename="a.wav", prompt="custom instruction"
        )
        parts = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
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
        client.chat.completions.create.assert_not_called()


class TestSynthesize:
    def test_calls_audio_speech_and_returns_bytes(self):
        client = MagicMock()
        speech = MagicMock()
        speech.read.return_value = b"RIFF...wavbytes"
        client.audio.speech.create.return_value = speech
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client)
        res = audio.synthesize(registry=reg, alias="voice", text="hi", voice="nova")
        assert res.audio_bytes == b"RIFF...wavbytes"
        assert res.media_type == "audio/mpeg"
        assert res.model_alias == "voice"
        kwargs = client.audio.speech.create.call_args.kwargs
        assert kwargs["voice"] == "nova"
        assert kwargs["input"] == "hi"

    def test_default_voice_when_empty(self):
        client = MagicMock()
        client.audio.speech.create.return_value = MagicMock(read=lambda: b"a")
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client)
        audio.synthesize(registry=reg, alias="voice", text="hi", voice="")
        assert client.audio.speech.create.call_args.kwargs["voice"] == "alloy"

    def test_backend_failure_raises_backend_error(self):
        client = MagicMock()
        client.audio.speech.create.side_effect = RuntimeError("down")
        reg = _FakeRegistry("voice", _Cfg("gpt-4o-mini-tts"), client)
        with pytest.raises(audio.AudioBackendError):
            audio.synthesize(registry=reg, alias="voice", text="hi", voice="nova")


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
    """The memoized, non-raising transcribe used by the no-native-audio wire
    fallback.  Caching an STT result is an audio-domain concern, so it lives here
    next to ``transcribe`` rather than bundled with PDF text extraction."""

    def _result(self, text: str):
        return audio.TranscriptionResult(transcript=text, model_alias="w", model="m")

    def test_memoizes_by_alias_and_hash(self, monkeypatch):
        audio._clear_transcript_cache_for_test()
        calls = []

        def fake(*, registry, alias, data, filename):
            calls.append(1)
            return self._result("hello world")

        monkeypatch.setattr(audio, "transcribe", fake)
        kw = dict(registry=object(), alias="w", content_hash="h1", data=b"x", filename="a.wav")
        assert audio.transcribe_cached(**kw) == "hello world"
        assert audio.transcribe_cached(**kw) == "hello world"
        assert len(calls) == 1  # second served from cache

    def test_backend_failure_returns_empty_and_is_not_cached(self, monkeypatch):
        audio._clear_transcript_cache_for_test()
        calls = []

        def boom(*, registry, alias, data, filename):
            calls.append(1)
            raise audio.AudioBackendError("down")

        monkeypatch.setattr(audio, "transcribe", boom)
        kw = dict(registry=object(), alias="w", content_hash="h2", data=b"x", filename="a.wav")
        assert audio.transcribe_cached(**kw) == ""
        audio.transcribe_cached(**kw)
        assert len(calls) == 2  # failure not cached -> retried


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
        client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="hi"))]
        )
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
        kwargs = client.chat.completions.create.call_args.kwargs
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
        client.audio.transcriptions.create.return_value = MagicMock(text="  full transcript  ")
        cfg = _Cfg("whisper-1")  # name inference -> dedicated endpoint, no chat stream
        gen = audio.transcribe_stream(
            registry=_FakeRegistry("w", cfg, client), alias="w", data=b"x"
        )
        assert list(gen) == ["full transcript"]
        client.chat.completions.create.assert_not_called()
