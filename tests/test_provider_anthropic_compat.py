"""Tests for the ``anthropic-compatible`` provider lane.

Local servers (vLLM) expose Anthropic's ``/v1/messages`` wire surface for
arbitrary checkpoints.  The lane reuses ``AnthropicProvider`` with
``compat=True``: identical message translation, but capabilities come from
``_ANTHROPIC_COMPAT_DEFAULT`` for every model (the static Claude table
never applies), native server-side tools are not injected, and operator
``server_compat["extra_body"]`` overrides ride the Anthropic SDK's
``extra_body`` — the channel for vLLM's ``chat_template_kwargs`` reasoning
toggle.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session as _make_session
from turnstone.core.providers._anthropic import AnthropicProvider
from turnstone.core.providers._protocol import ModelCapabilities

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_client() -> MagicMock:
    """Build a fake Anthropic client whose ``messages.stream`` records kwargs."""
    stream_ctx = MagicMock()
    stream_ctx.__enter__ = MagicMock(return_value=iter([]))
    stream_ctx.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.messages.stream.return_value = stream_ctx
    return client


_WEB_SEARCH_FUNCTION_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


# ===========================================================================
# TestCompatCapabilities
# ===========================================================================


class TestCompatCapabilities:
    """Capability resolution on the compat lane."""

    def test_compat_capability_defaults(self) -> None:
        provider = AnthropicProvider(compat=True)
        caps = provider.get_capabilities("deepseek-ai/DeepSeek-V4-Flash")
        assert caps.token_param == "max_tokens"
        assert caps.thinking_mode == "none"
        assert caps.supports_web_search is False
        assert caps.supports_tool_search is False
        assert caps.supports_vision is False
        assert caps.supports_reasoning_replay is True
        assert caps.supports_temperature is True

    def test_claude_id_does_not_pick_up_static_table(self) -> None:
        """A Claude-named local checkpoint must not inherit Claude API caps."""
        provider = AnthropicProvider(compat=True)
        caps = provider.get_capabilities("claude-opus-4-6")
        assert caps.context_window == 200000
        assert caps.thinking_mode == "none"
        assert caps.supports_web_search is False
        # The real lane still resolves the static entry.
        real_caps = AnthropicProvider().get_capabilities("claude-opus-4-6")
        assert real_caps.context_window == 1000000
        assert real_caps.thinking_mode == "adaptive"


# ===========================================================================
# TestCompatWireShape
# ===========================================================================


class TestCompatWireShape:
    """Body-inspecting tests on the kwargs handed to ``messages.stream``."""

    def setup_method(self) -> None:
        self.provider = AnthropicProvider(compat=True)

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_compat_no_web_search_swap_no_temp_force(self, mock_ensure: MagicMock) -> None:
        """No native web_search swap, no temperature=1 forcing, max_tokens param."""
        client = _capture_client()
        list(
            self.provider.create_streaming(
                client=client,
                model="deepseek-ai/DeepSeek-V4-Flash",
                messages=[{"role": "user", "content": "hi"}],
                tools=[_WEB_SEARCH_FUNCTION_TOOL],
                temperature=0.6,
            )
        )
        kwargs = client.messages.stream.call_args[1]
        sent_tools = kwargs["tools"]
        assert sent_tools == [
            {
                "name": "web_search",
                "description": "Search the web",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]
        assert all(t.get("type") != "web_search_20250305" for t in sent_tools)
        assert kwargs["temperature"] == 0.6
        assert "thinking" not in kwargs
        assert "max_tokens" in kwargs
        assert "max_completion_tokens" not in kwargs

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_extra_params_passthrough_to_extra_body(self, mock_ensure: MagicMock) -> None:
        """server_compat extra_body (chat_template_kwargs) reaches the SDK."""
        client = _capture_client()
        list(
            self.provider.create_streaming(
                client=client,
                model="deepseek-ai/DeepSeek-V4-Flash",
                messages=[{"role": "user", "content": "hi"}],
                extra_params={"chat_template_kwargs": {"thinking": False}},
            )
        )
        kwargs = client.messages.stream.call_args[1]
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"thinking": False}}

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_internal_keys_not_leaked(self, mock_ensure: MagicMock) -> None:
        """Real-lane request bodies stay byte-identical with thinking overrides.

        ``thinking_budget_tokens`` is consumed by ``_reasoning_params`` and
        must never surface as wire ``extra_body`` — a leaked key would change
        every real-Anthropic request that threads a thinking override.
        Negative-tested: fails when the ``_INTERNAL_EXTRA_PARAMS`` exclusion
        is removed from ``_build_thinking_and_kwargs``.  The effort knob is
        explicit: unset effort means thinking OFF (the budget override
        modifies a thinking block, it never creates one).
        """
        provider = AnthropicProvider()
        client = _capture_client()
        list(
            provider.create_streaming(
                client=client,
                model="claude-sonnet-4-5",
                messages=[{"role": "user", "content": "hi"}],
                reasoning_effort="medium",
                extra_params={"thinking_budget_tokens": 2048},
            )
        )
        kwargs = client.messages.stream.call_args[1]
        assert "extra_body" not in kwargs
        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 2048}


# ===========================================================================
# TestCompatReasoningControl
# ===========================================================================


class TestCompatReasoningControl:
    """Session effort knob → ``chat_template_kwargs`` on the compat lane.

    vLLM's ``/v1/messages`` ignores the native ``thinking`` param — the
    reasoning levers live in the chat template.
    ``merge_reasoning_template_kwargs`` maps the knob onto
    ``caps.thinking_param`` (manual: knob "none" = off, mirroring
    ``_reasoning_params``; adaptive: always on) and ``caps.effort_param``
    (graded value for gpt-oss-style templates).  Verified live against
    qwen3.6 on vLLM 2026-07-03: ``{"enable_thinking": false}`` disables
    thinking, unknown chat_template_kwargs keys are silently ignored.
    """

    _MANUAL_CAPS = ModelCapabilities(
        token_param="max_tokens",
        thinking_mode="manual",
        thinking_param="enable_thinking",
    )

    def setup_method(self) -> None:
        self.provider = AnthropicProvider(compat=True)

    def _stream_kwargs(
        self,
        caps: ModelCapabilities | None,
        reasoning_effort: str,
        extra_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = _capture_client()
        with patch("turnstone.core.providers._anthropic._ensure_anthropic"):
            list(
                self.provider.create_streaming(
                    client=client,
                    model="qwen3.6-27b",
                    messages=[{"role": "user", "content": "hi"}],
                    temperature=0.6,
                    reasoning_effort=reasoning_effort,
                    extra_params=extra_params,
                    capabilities=caps,
                )
            )
        return client.messages.stream.call_args[1]

    def test_manual_toggle_on(self) -> None:
        """Any non-none effort turns the toggle on AND carries the graded
        value under the fallback key — the user's effort setting always
        reaches the wire; a template that doesn't reference the kwarg
        ignores it."""
        kwargs = self._stream_kwargs(self._MANUAL_CAPS, "medium")
        assert kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "medium"}
        }
        assert "thinking" not in kwargs
        assert kwargs["temperature"] == 0.6  # never forced to 1.0 on compat

    def test_manual_toggle_explicit_off(self) -> None:
        """The explicit "none" knob disables thinking — native manual-mode
        parity; no effort key rides when thinking is off."""
        kwargs = self._stream_kwargs(self._MANUAL_CAPS, "none")
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
        assert "thinking" not in kwargs

    def test_manual_unset_injects_nothing(self) -> None:
        """An UNSET knob (no rung of the assignment scheme resolved a
        value) injects no toggle at all — the template's own default
        rules, matching "if not set, we don't send it".  Distinct from
        the explicit "none" off-switch above."""
        kwargs = self._stream_kwargs(self._MANUAL_CAPS, "")
        assert "extra_body" not in kwargs
        assert "thinking" not in kwargs

    def test_adaptive_always_on(self) -> None:
        """Adaptive never knob-disables — native-adaptive contract, no native
        dict; the graded value rides for on-positions only."""
        caps = dataclasses.replace(self._MANUAL_CAPS, thinking_mode="adaptive")
        kwargs = self._stream_kwargs(caps, "high")
        assert kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "high"}
        }
        assert "thinking" not in kwargs
        assert kwargs["temperature"] == 0.6
        kwargs = self._stream_kwargs(caps, "none")
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
        assert "thinking" not in kwargs
        assert kwargs["temperature"] == 0.6

    def test_default_caps_inject_nothing(self) -> None:
        """Untouched compat defaults (thinking_mode=none) keep today's wire."""
        kwargs = self._stream_kwargs(None, "medium")
        assert "extra_body" not in kwargs
        assert "thinking" not in kwargs

    def test_effort_param_validated_against_values(self) -> None:
        """Off-list knob rounds up onto the declared values (ceiling-capped),
        never sent raw — and never snaps DOWN to the default."""
        caps = dataclasses.replace(
            self._MANUAL_CAPS,
            effort_param="reasoning_effort",
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        )
        kwargs = self._stream_kwargs(caps, "xhigh")
        assert kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "high"}
        }

    def test_effort_param_freeform_without_values(self) -> None:
        """No declared values → knob forwarded as-is, template is authority."""
        caps = ModelCapabilities(
            token_param="max_tokens",
            effort_param="reasoning_effort",
        )
        kwargs = self._stream_kwargs(caps, "xhigh")
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"reasoning_effort": "xhigh"}}

    def test_effort_param_omitted_on_none(self) -> None:
        """Knob "none" sends no effort key (and toggles thinking off)."""
        caps = dataclasses.replace(
            self._MANUAL_CAPS,
            effort_param="reasoning_effort",
            reasoning_effort_values=("low", "medium", "high"),
        )
        kwargs = self._stream_kwargs(caps, "none")
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}

    def test_operator_override_wins(self) -> None:
        """server_compat chat_template_kwargs entries beat the knob mapping."""
        kwargs = self._stream_kwargs(
            self._MANUAL_CAPS,
            "none",
            extra_params={"chat_template_kwargs": {"enable_thinking": True}, "foo": 1},
        )
        assert kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": True},
            "foo": 1,
        }

    def test_caller_extra_params_not_mutated(self) -> None:
        """The session's extra_params dict must never be written through."""
        extra = {"chat_template_kwargs": {"foo": 1}}
        self._stream_kwargs(self._MANUAL_CAPS, "medium", extra_params=extra)
        assert extra == {"chat_template_kwargs": {"foo": 1}}

    def test_no_output_config_on_compat(self) -> None:
        """supports_effort must not leak Anthropic output_config to vLLM."""
        caps = dataclasses.replace(
            self._MANUAL_CAPS,
            supports_effort=True,
            effort_levels=("low", "medium", "high"),
        )
        kwargs = self._stream_kwargs(caps, "high")
        assert "output_config" not in kwargs
        assert kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "high"}
        }

    def test_create_streaming_same_injection(self) -> None:
        """The public streaming entry shares _build_thinking_and_kwargs."""
        client = MagicMock()
        client.messages.stream.return_value.__enter__.return_value = iter([])
        with patch("turnstone.core.providers._anthropic._ensure_anthropic"):
            list(
                self.provider.create_streaming(
                    client=client,
                    model="qwen3.6-27b",
                    messages=[{"role": "user", "content": "hi"}],
                    reasoning_effort="none",
                    capabilities=self._MANUAL_CAPS,
                )
            )
        kwargs = client.messages.stream.call_args[1]
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


# ===========================================================================
# TestCompatFactory
# ===========================================================================


class TestCompatFactory:
    """create_provider / create_client routing for the compat lane."""

    def test_create_provider_anthropic_compatible(self) -> None:
        from turnstone.core.providers import create_provider

        provider = create_provider("anthropic-compatible")
        assert provider.provider_name == "anthropic-compatible"
        assert provider is not create_provider("anthropic")
        assert create_provider("anthropic-compatible") is provider

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_create_client_anthropic_compatible(self, mock_ensure: MagicMock) -> None:
        """base_url forwards verbatim; empty api_key is omitted entirely."""
        from turnstone.core.providers import create_client

        mock_anthropic_cls = MagicMock()
        mock_mod = MagicMock()
        mock_mod.Anthropic = mock_anthropic_cls
        mock_ensure.return_value = mock_mod

        create_client("anthropic-compatible", base_url="http://vllm-host:8000", api_key="")
        mock_anthropic_cls.assert_called_once_with(base_url="http://vllm-host:8000")

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_create_client_strips_v1_suffix(self, mock_ensure: MagicMock) -> None:
        """A /v1-suffixed base_url (openai-compatible muscle memory) is
        normalized for the compat lane — the SDK appends /v1/... itself,
        so the verbatim URL would request /v1/v1/messages and 404."""
        from turnstone.core.providers import create_client

        mock_anthropic_cls = MagicMock()
        mock_mod = MagicMock()
        mock_mod.Anthropic = mock_anthropic_cls
        mock_ensure.return_value = mock_mod

        for suffixed in ("http://vllm-host:8000/v1", "http://vllm-host:8000/v1/"):
            mock_anthropic_cls.reset_mock()
            create_client("anthropic-compatible", base_url=suffixed, api_key="")
            mock_anthropic_cls.assert_called_once_with(base_url="http://vllm-host:8000")

        # A base_url that strips to nothing stays verbatim so the typo
        # fails loudly in httpx instead of silently targeting the SDK's
        # prod default.
        mock_anthropic_cls.reset_mock()
        create_client("anthropic-compatible", base_url="/v1", api_key="")
        mock_anthropic_cls.assert_called_once_with(base_url="/v1")

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_create_client_requires_base_url(self, mock_ensure: MagicMock) -> None:
        """Empty base_url fails at construction — the local-only lane must
        never fall back to the SDK's https://api.anthropic.com default."""
        from turnstone.core.providers import create_client

        with pytest.raises(ValueError, match="anthropic-compatible requires base_url"):
            create_client("anthropic-compatible", base_url="", api_key="dummy")
        mock_ensure.return_value.Anthropic.assert_not_called()

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_create_client_real_lane_base_url_untouched(self, mock_ensure: MagicMock) -> None:
        """The real anthropic lane forwards base_url verbatim — the /v1
        normalization is compat-lane-only."""
        from turnstone.core.providers import create_client

        mock_anthropic_cls = MagicMock()
        mock_mod = MagicMock()
        mock_mod.Anthropic = mock_anthropic_cls
        mock_ensure.return_value = mock_mod

        create_client("anthropic", base_url="http://proxy:9000/v1", api_key="k")
        mock_anthropic_cls.assert_called_once_with(api_key="k", base_url="http://proxy:9000/v1")


# ===========================================================================
# TestCliScope
# ===========================================================================


class TestCliScope:
    def test_cli_rejects_compat_provider_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The lane is registry-only — the CLI --provider flag does not grow."""
        from turnstone import cli

        monkeypatch.setattr(sys, "argv", ["turnstone", "--provider", "anthropic-compatible"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2


# ===========================================================================
# TestCompatSessionPlumbing
# ===========================================================================


class TestCompatSessionPlumbing:
    """ChatSession capability merge + extra_params gate for the lane."""

    def test_per_model_capability_override_merge(self, tmp_db: Any) -> None:
        """Per-model capabilities win over _ANTHROPIC_COMPAT_DEFAULT fields."""
        from turnstone.core.model_registry import ModelConfig, ModelRegistry
        from turnstone.core.providers import create_provider

        cfg = ModelConfig(
            alias="vllm-messages",
            base_url="http://localhost:8000",
            api_key="dummy",
            model="deepseek-ai/DeepSeek-V4-Flash",
            provider="anthropic-compatible",
            capabilities={"supports_mid_conversation_system": True, "context_window": 131072},
        )
        registry = ModelRegistry(models={"vllm-messages": cfg}, default="vllm-messages")
        session = _make_session(registry=registry, model_alias="vllm-messages")
        provider = create_provider("anthropic-compatible")
        caps = session._resolve_capabilities(
            provider, "deepseek-ai/DeepSeek-V4-Flash", "vllm-messages"
        )
        assert caps.supports_mid_conversation_system is True
        assert caps.context_window == 131072
        # Untouched fields keep the compat-lane defaults.
        assert caps.token_param == "max_tokens"
        assert caps.thinking_mode == "none"
        assert caps.supports_web_search is False
        assert caps.supports_vision is False

    def test_session_extra_params_gate(self, tmp_db: Any) -> None:
        """server_compat extra_body forwards for the compat lane, not real Anthropic."""
        from turnstone.core.model_registry import ModelConfig, ModelRegistry
        from turnstone.core.providers import create_provider

        session = _make_session(reasoning_effort="medium")
        cfg = ModelConfig(
            alias="vllm-messages",
            base_url="http://localhost:8000",
            api_key="dummy",
            model="deepseek-ai/DeepSeek-V4-Flash",
            provider="anthropic-compatible",
            server_compat={"extra_body": {"chat_template_kwargs": {"thinking": False}}},
        )
        session._registry = ModelRegistry(models={"vllm-messages": cfg}, default="vllm-messages")
        session._model_alias = "vllm-messages"

        session._provider = create_provider("anthropic-compatible")
        assert session._provider_extra_params() == {"chat_template_kwargs": {"thinking": False}}

        session._provider = create_provider("anthropic")
        assert session._provider_extra_params() is None


# ===========================================================================
# TestLiveCompatStream
# ===========================================================================


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("TURNSTONE_LIVE_ANTHROPIC_COMPAT_URL"),
    reason="TURNSTONE_LIVE_ANTHROPIC_COMPAT_URL not set",
)
class TestLiveCompatStream:
    """One real streamed turn against a vLLM /v1/messages endpoint."""

    def test_live_compat_streamed_turn(self) -> None:
        from turnstone.core.providers import create_client, create_provider

        base_url = os.environ["TURNSTONE_LIVE_ANTHROPIC_COMPAT_URL"]
        model = os.environ.get(
            "TURNSTONE_LIVE_ANTHROPIC_COMPAT_MODEL", "deepseek-ai/DeepSeek-V4-Flash"
        )
        client = create_client("anthropic-compatible", base_url=base_url, api_key="dummy")
        provider = create_provider("anthropic-compatible")
        chunks = list(
            provider.create_streaming(
                client=client,
                model=model,
                messages=[{"role": "user", "content": "Reply with the single word: pong"}],
                max_tokens=64,
                extra_params={"chat_template_kwargs": {"thinking": False}},
            )
        )
        content = "".join(c.content_delta or "" for c in chunks)
        assert content.strip()
        assert any(c.finish_reason for c in chunks)
        assert any(c.usage is not None for c in chunks)
        assert not any(c.reasoning_delta for c in chunks)
