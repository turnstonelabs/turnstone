"""Tests for probe_model_endpoint() and lookup_model_capabilities()."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from turnstone.core.model_registry import probe_model_endpoint
from turnstone.core.providers import list_known_models, lookup_model_capabilities

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_model(
    model_id: str,
    *,
    owned_by: str = "test",
    meta: dict[str, Any] | None = None,
) -> MagicMock:
    m = MagicMock()
    m.id = model_id
    dumped: dict[str, Any] = {"owned_by": owned_by}
    if meta is not None:
        dumped["meta"] = meta
    m.model_dump.return_value = dumped
    return m


def _mock_client(*models: MagicMock) -> MagicMock:
    fast = MagicMock()
    fast.models.list.return_value = MagicMock(data=list(models))
    client = MagicMock()
    client.with_options.return_value = fast
    return client


# ---------------------------------------------------------------------------
# probe_model_endpoint
# ---------------------------------------------------------------------------


class TestProbeModelEndpoint:
    @patch("turnstone.core.providers.create_client")
    def test_probe_success(self, mock_cc: MagicMock) -> None:
        m1 = _mock_model("model-a")
        m2 = _mock_model("model-b")
        mock_cc.return_value = _mock_client(m1, m2)

        result = probe_model_endpoint("openai", "http://localhost:8000/v1", "key")
        assert result["reachable"] is True
        assert result["available_models"] == ["model-a", "model-b"]
        assert result["error"] is None

    @patch("turnstone.core.providers.create_client")
    def test_target_found(self, mock_cc: MagicMock) -> None:
        m1 = _mock_model("gpt-5")
        mock_cc.return_value = _mock_client(m1)

        result = probe_model_endpoint(
            "openai", "http://localhost:8000/v1", "key", target_model="gpt-5"
        )
        assert result["model_found"] is True

    @patch("turnstone.core.providers.create_client")
    def test_target_not_found(self, mock_cc: MagicMock) -> None:
        m1 = _mock_model("model-a")
        mock_cc.return_value = _mock_client(m1)

        result = probe_model_endpoint(
            "openai", "http://localhost:8000/v1", "key", target_model="gpt-5"
        )
        assert result["model_found"] is False
        assert result["available_models"] == ["model-a"]

    @patch("turnstone.core.providers.create_client")
    def test_no_target_model_found_is_none(self, mock_cc: MagicMock) -> None:
        m1 = _mock_model("model-a")
        mock_cc.return_value = _mock_client(m1)

        result = probe_model_endpoint("openai", "http://localhost:8000/v1", "key")
        assert result["model_found"] is None

    @patch("turnstone.core.providers.create_client")
    def test_context_window_llama_cpp(self, mock_cc: MagicMock) -> None:
        m = _mock_model("qwen-32b", meta={"n_ctx_train": 131072})
        mock_cc.return_value = _mock_client(m)

        result = probe_model_endpoint("openai", "http://localhost:8000/v1", "key")
        assert result["context_window"] == 131072
        assert result["server_type"] == "llama.cpp"

    @patch("turnstone.core.providers.create_client")
    def test_server_type_openai(self, mock_cc: MagicMock) -> None:
        m = _mock_model("gpt-5")
        mock_cc.return_value = _mock_client(m)

        result = probe_model_endpoint("openai", "https://api.openai.com/v1", "sk-test")
        assert result["server_type"] == "openai"

    @patch("turnstone.core.providers.create_client")
    def test_server_type_sglang(self, mock_cc: MagicMock) -> None:
        m = _mock_model("meta-llama/Llama-3", owned_by="sglang")
        mock_cc.return_value = _mock_client(m)

        result = probe_model_endpoint("openai", "http://localhost:30000/v1", "key")
        assert result["server_type"] == "sglang"

    @patch("turnstone.core.providers.create_client")
    def test_server_type_vllm(self, mock_cc: MagicMock) -> None:
        m = _mock_model("org/model-name")
        mock_cc.return_value = _mock_client(m)

        result = probe_model_endpoint("openai", "http://localhost:8000/v1", "key")
        assert result["server_type"] == "vllm"

    @patch("turnstone.core.providers.create_client")
    def test_server_type_generic(self, mock_cc: MagicMock) -> None:
        m = _mock_model("my-model")
        mock_cc.return_value = _mock_client(m)

        result = probe_model_endpoint("openai", "http://localhost:8000/v1", "key")
        assert result["server_type"] == "openai-compatible"

    @patch("turnstone.core.providers.create_client")
    def test_anthropic_provider(self, mock_cc: MagicMock) -> None:
        m = _mock_model("claude-sonnet-4-6")
        mock_cc.return_value = _mock_client(m)

        result = probe_model_endpoint(
            "anthropic",
            "https://api.anthropic.com",
            "sk-ant-test",
            target_model="claude-sonnet-4-6",
        )
        assert result["reachable"] is True
        assert result["server_type"] == "anthropic"
        assert result["context_window"] == 200000

    @patch("turnstone.core.providers.create_client")
    def test_connection_failure(self, mock_cc: MagicMock) -> None:
        mock_cc.side_effect = OSError("Connection refused")

        result = probe_model_endpoint("openai", "http://bad:1234/v1", "key")
        assert result["reachable"] is False
        assert "Connection refused" in (result["error"] or "")

    @patch("turnstone.core.providers.create_client")
    def test_empty_model_list(self, mock_cc: MagicMock) -> None:
        mock_cc.return_value = _mock_client()  # no models

        result = probe_model_endpoint("openai", "http://localhost:8000/v1", "key")
        assert result["reachable"] is True
        assert result["available_models"] == []
        assert "No models found" in (result["error"] or "")

    @patch("turnstone.core.providers.create_client")
    def test_context_window_openai_static_table(self, mock_cc: MagicMock) -> None:
        """When base_url is api.openai.com and model is known, use static table."""
        m = _mock_model("gpt-5")
        mock_cc.return_value = _mock_client(m)

        result = probe_model_endpoint(
            "openai", "https://api.openai.com/v1", "sk-test", target_model="gpt-5"
        )
        assert result["context_window"] == 400000


# ---------------------------------------------------------------------------
# lookup_model_capabilities
# ---------------------------------------------------------------------------


class TestLookupModelCapabilities:
    def test_known_openai_model(self) -> None:
        caps = lookup_model_capabilities("openai", "gpt-5")
        assert caps is not None
        assert caps["context_window"] == 400000
        assert caps["supports_temperature"] is False

    def test_known_anthropic_model(self) -> None:
        caps = lookup_model_capabilities("anthropic", "claude-opus-4-6")
        assert caps is not None
        assert caps["context_window"] == 200000
        assert caps["thinking_mode"] == "adaptive"

    def test_unknown_model_returns_none(self) -> None:
        caps = lookup_model_capabilities("openai", "totally-unknown-model")
        assert caps is None

    def test_tuples_converted_to_lists(self) -> None:
        caps = lookup_model_capabilities("openai", "gpt-5")
        assert caps is not None
        for val in caps.values():
            assert not isinstance(val, tuple), f"Found tuple: {val}"

    def test_reasoning_effort_values_are_list(self) -> None:
        caps = lookup_model_capabilities("openai", "gpt-5")
        assert caps is not None
        assert isinstance(caps["reasoning_effort_values"], list)
        assert "medium" in caps["reasoning_effort_values"]

    def test_openai_compatible_returns_none(self) -> None:
        caps = lookup_model_capabilities("openai-compatible", "my-local-model")
        assert caps is None

    def test_invalid_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            lookup_model_capabilities("bad-provider", "gpt-5")


# ---------------------------------------------------------------------------
# list_known_models
# ---------------------------------------------------------------------------


class TestListKnownModels:
    def test_openai_models(self) -> None:
        models = list_known_models("openai")
        assert "gpt-5" in models
        assert isinstance(models, list)
        assert models == sorted(models)

    def test_anthropic_models(self) -> None:
        models = list_known_models("anthropic")
        assert "claude-opus-4-6" in models

    def test_openai_compatible_returns_empty(self) -> None:
        assert list_known_models("openai-compatible") == []

    def test_unknown_provider_returns_empty(self) -> None:
        assert list_known_models("bad-provider") == []
