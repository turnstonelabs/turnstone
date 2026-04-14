"""Tests for turnstone.core.server_compat — profile suggestion and merging."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.server_compat import merge_server_compat, suggest_profile

# ---------------------------------------------------------------------------
# suggest_profile
# ---------------------------------------------------------------------------


class TestSuggestProfile:
    def test_vllm_gemma4(self) -> None:
        p = suggest_profile("vllm", "google/gemma-4-31B-it")
        assert p["capabilities"]["thinking_mode"] == "manual"
        assert p["capabilities"]["thinking_param"] == "enable_thinking"
        assert p["server_compat"]["extra_body"]["skip_special_tokens"] is False

    def test_vllm_gemma3(self) -> None:
        p = suggest_profile("vllm", "google/gemma-3-27b-it")
        assert p["capabilities"]["thinking_mode"] == "manual"

    def test_vllm_qwen3(self) -> None:
        p = suggest_profile("vllm", "Qwen/Qwen3-8B")
        assert p["capabilities"]["thinking_mode"] == "manual"
        assert p["capabilities"]["thinking_param"] == "enable_thinking"
        # Qwen doesn't need skip_special_tokens workaround
        assert "extra_body" not in p.get("server_compat", {})

    def test_vllm_qwq(self) -> None:
        p = suggest_profile("vllm", "Qwen/QwQ-32B")
        assert p["capabilities"]["thinking_mode"] == "manual"

    def test_vllm_granite(self) -> None:
        p = suggest_profile("vllm", "ibm-granite/granite-3.2-2b-instruct")
        assert p["capabilities"]["thinking_param"] == "thinking"

    def test_vllm_deepseek_r1(self) -> None:
        p = suggest_profile("vllm", "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B")
        assert p["capabilities"]["thinking_param"] == "thinking"

    def test_vllm_deepseek_v3_no_thinking(self) -> None:
        """DeepSeek-V3 is a chat model, not a reasoning model — no thinking profile."""
        p = suggest_profile("vllm", "deepseek-ai/DeepSeek-V3-0324")
        assert "capabilities" not in p
        assert p["server_compat"]["server_type"] == "vllm"

    def test_vllm_non_thinking_model(self) -> None:
        p = suggest_profile("vllm", "meta-llama/Llama-3-70B-Instruct")
        assert "capabilities" not in p
        assert p["server_compat"]["server_type"] == "vllm"

    def test_llama_cpp_non_thinking(self) -> None:
        p = suggest_profile("llama.cpp", "some-model")
        assert p["server_compat"]["server_type"] == "llama.cpp"
        assert "capabilities" not in p

    def test_llama_cpp_gemma_thinking(self) -> None:
        """llama.cpp with Gemma model gets thinking profile with reasoning_format."""
        p = suggest_profile("llama.cpp", "gemma-4-E4B-it.gguf")
        assert p["capabilities"]["thinking_mode"] == "manual"
        assert p["server_compat"]["extra_body"]["reasoning_format"] == "auto"

    def test_llama_cpp_qwen_thinking(self) -> None:
        p = suggest_profile("llama.cpp", "Qwen3-8B-Q4_K_M.gguf")
        assert p["capabilities"]["thinking_mode"] == "manual"

    def test_sglang(self) -> None:
        p = suggest_profile("sglang", "some-model")
        assert p["server_compat"]["server_type"] == "sglang"

    def test_unknown_server(self) -> None:
        assert suggest_profile("unknown", "foo") == {}

    def test_empty_inputs(self) -> None:
        assert suggest_profile("", "") == {}

    def test_openai_compatible_fallback(self) -> None:
        """Generic openai-compatible without a specific profile."""
        assert suggest_profile("openai-compatible", "some-local-model") == {}

    def test_case_insensitive_model_match(self) -> None:
        """Model matching should be case-insensitive."""
        p = suggest_profile("vllm", "Google/GEMMA-4-31B-IT")
        assert p["capabilities"]["thinking_mode"] == "manual"

    def test_holo_requires_holo2(self) -> None:
        """Short 'holo' prefix shouldn't false-match; 'holo2' should match."""
        p_short = suggest_profile("vllm", "some-org/hologram-7b")
        assert "capabilities" not in p_short
        p_long = suggest_profile("vllm", "some-org/Holo2-14B")
        assert p_long["capabilities"]["thinking_mode"] == "manual"

    def test_suggest_returns_deep_copy(self) -> None:
        """Mutating the returned profile should not affect future calls."""
        p1 = suggest_profile("vllm", "google/gemma-4-31B-it")
        p1["capabilities"]["thinking_mode"] = "none"
        p2 = suggest_profile("vllm", "google/gemma-4-31B-it")
        assert p2["capabilities"]["thinking_mode"] == "manual"


# ---------------------------------------------------------------------------
# merge_server_compat
# ---------------------------------------------------------------------------


class TestMergeServerCompat:
    def test_empty_compat_returns_base_only(self) -> None:
        base = {"reasoning_effort": "medium"}
        result = merge_server_compat(base, {})
        assert result == {"chat_template_kwargs": {"reasoning_effort": "medium"}}

    def test_extra_body_merged_top_level(self) -> None:
        base = {"reasoning_effort": "medium"}
        compat = {"extra_body": {"skip_special_tokens": False}}
        result = merge_server_compat(base, compat)
        assert result["skip_special_tokens"] is False
        assert "chat_template_kwargs" in result

    def test_full_vllm_gemma_compat(self) -> None:
        base = {"reasoning_effort": "medium"}
        compat = {
            "server_type": "vllm",
            "extra_body": {"skip_special_tokens": False},
        }
        result = merge_server_compat(base, compat)
        assert result == {
            "chat_template_kwargs": {"reasoning_effort": "medium"},
            "skip_special_tokens": False,
        }

    def test_extra_body_chat_template_kwargs_ignored(self) -> None:
        """extra_body should not contain chat_template_kwargs — it's skipped."""
        base = {"reasoning_effort": "medium"}
        compat = {
            "extra_body": {
                "chat_template_kwargs": {"should_be_ignored": True},
                "skip_special_tokens": False,
            },
        }
        result = merge_server_compat(base, compat)
        assert "should_be_ignored" not in result.get("chat_template_kwargs", {})
        assert result["skip_special_tokens"] is False

    def test_base_not_mutated(self) -> None:
        base = {"reasoning_effort": "medium"}
        compat = {"extra_body": {"skip_special_tokens": False}}
        merge_server_compat(base, compat)
        assert "skip_special_tokens" not in base

    def test_non_dict_extra_body_ignored(self) -> None:
        """Gracefully handle malformed server_compat."""
        base = {"reasoning_effort": "medium"}
        result = merge_server_compat(base, {"extra_body": 42})
        assert result == {"chat_template_kwargs": {"reasoning_effort": "medium"}}


# ---------------------------------------------------------------------------
# End-to-end: session merge + provider thinking mode
# ---------------------------------------------------------------------------


class TestEndToEndRequestShaping:
    """Compose both layers — session builds extra_params, provider applies thinking."""

    def test_vllm_gemma_full_flow(self) -> None:
        """Session merges server workarounds, provider adds thinking param."""
        caps = ModelCapabilities(thinking_mode="manual", thinking_param="enable_thinking")
        base_ctk = {"reasoning_effort": "medium"}
        server_compat = {
            "server_type": "vllm",
            "extra_body": {"skip_special_tokens": False},
        }
        # Step 1: session merges
        extra_params = merge_server_compat(base_ctk, server_compat)
        # Step 2: provider finalises
        extra_body = dict(extra_params)
        OpenAIChatCompletionsProvider._apply_thinking_mode(extra_body, caps)

        assert extra_body == {
            "chat_template_kwargs": {
                "reasoning_effort": "medium",
                "enable_thinking": True,
            },
            "skip_special_tokens": False,
        }

    def test_granite_thinking_key(self) -> None:
        """Granite uses 'thinking' instead of 'enable_thinking'."""
        caps = ModelCapabilities(thinking_mode="manual", thinking_param="thinking")
        extra_params = merge_server_compat({"reasoning_effort": "low"}, {})
        extra_body = dict(extra_params)
        OpenAIChatCompletionsProvider._apply_thinking_mode(extra_body, caps)

        assert extra_body["chat_template_kwargs"]["thinking"] is True
        assert "enable_thinking" not in extra_body["chat_template_kwargs"]

    def test_non_thinking_model_no_injection(self) -> None:
        """Non-thinking model gets no thinking params."""
        caps = ModelCapabilities()  # thinking_mode="none"
        extra_params = merge_server_compat({"reasoning_effort": "medium"}, {})
        extra_body = dict(extra_params)
        OpenAIChatCompletionsProvider._apply_thinking_mode(extra_body, caps)

        assert extra_body == {"chat_template_kwargs": {"reasoning_effort": "medium"}}


# ---------------------------------------------------------------------------
# Probe integration: suggest_profile called from _detect_openai_compat
# ---------------------------------------------------------------------------


class TestProbeIntegration:
    def test_detect_vllm_gemma_suggests_profile(self) -> None:
        """_detect_openai_compat returns suggested_capabilities and suggested_server_compat."""
        from turnstone.core.model_registry import _detect_openai_compat

        result: dict[str, Any] = {
            "reachable": True,
            "model_found": True,
            "available_models": ["google/gemma-4-31B-it"],
            "context_window": None,
            "server_type": None,
            "error": None,
        }
        model_obj = MagicMock()
        model_obj.model_dump.return_value = {"owned_by": "vllm"}

        _detect_openai_compat(
            result, model_obj, "google/gemma-4-31B-it", "http://localhost:8000/v1"
        )

        assert result["server_type"] == "vllm"
        assert result["suggested_capabilities"]["thinking_mode"] == "manual"
        assert result["suggested_capabilities"]["thinking_param"] == "enable_thinking"
        assert result["suggested_server_compat"]["extra_body"]["skip_special_tokens"] is False

    def test_detect_non_thinking_no_suggested_capabilities(self) -> None:
        """Non-thinking vLLM model gets server_compat but no capabilities suggestion."""
        from turnstone.core.model_registry import _detect_openai_compat

        result: dict[str, Any] = {
            "reachable": True,
            "model_found": True,
            "available_models": ["meta-llama/Llama-3-70B"],
            "context_window": None,
            "server_type": None,
            "error": None,
        }
        model_obj = MagicMock()
        model_obj.model_dump.return_value = {"owned_by": "vllm"}

        _detect_openai_compat(
            result, model_obj, "meta-llama/Llama-3-70B", "http://localhost:8000/v1"
        )

        assert result["server_type"] == "vllm"
        assert "suggested_capabilities" not in result
        assert result["suggested_server_compat"]["server_type"] == "vllm"
