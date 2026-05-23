"""Tests for turnstone.core.output_guard_judge."""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

from turnstone.core.judge import JudgeConfig
from turnstone.core.output_guard_judge import (
    OutputGuardJudge,
    OutputJudgeVerdict,
    _extract_json,
)


def _make_provider(
    content: str = "", *, delay: float = 0.0, raises: Exception | None = None
) -> Any:
    """Build a mock LLMProvider whose create_completion returns the given content."""
    provider = MagicMock()
    provider.provider_name = "openai"

    def _create_completion(**_kwargs: Any) -> Any:
        if delay:
            time.sleep(delay)
        if raises is not None:
            raise raises
        result = MagicMock()
        result.content = content
        return result

    provider.create_completion = _create_completion
    return provider


def _make_judge(
    *,
    content: str = "",
    timeout: float = 5.0,
    delay: float = 0.0,
    raises: Exception | None = None,
) -> OutputGuardJudge:
    """Construct an OutputGuardJudge wired to a mock provider."""
    provider = _make_provider(content, delay=delay, raises=raises)
    config = JudgeConfig(output_guard_llm=True, output_guard_llm_timeout=timeout)
    client = MagicMock()
    client.base_url = "http://test"
    client.api_key = "test-key"
    # Patch create_client so _create_client doesn't hit a real factory.
    from turnstone.core import output_guard_judge as ogj_mod

    ogj_mod_create = ogj_mod.OutputGuardJudge._create_client

    def _fake_create_client(self: OutputGuardJudge) -> Any:
        return client

    ogj_mod.OutputGuardJudge._create_client = _fake_create_client  # type: ignore[assignment]
    try:
        judge = OutputGuardJudge(
            config=config,
            session_provider=provider,
            session_client=client,
            session_model="test-model",
        )
    finally:
        ogj_mod.OutputGuardJudge._create_client = ogj_mod_create  # type: ignore[assignment]
    # Bind the un-patched factory back, but with a per-instance override:
    judge._create_client = lambda: client  # type: ignore[method-assign]
    return judge


class TestVerdictDataclass:
    def test_succeeded_default_is_false(self) -> None:
        v = OutputJudgeVerdict()
        # Default risk_level is "none" but error is "" so it succeeds.
        assert v.succeeded is True

    def test_error_makes_unsucceeded(self) -> None:
        v = OutputJudgeVerdict(risk_level="none", error="timeout")
        assert v.succeeded is False

    def test_invalid_risk_makes_unsucceeded(self) -> None:
        v = OutputJudgeVerdict(risk_level="bogus")
        assert v.succeeded is False


class TestEvaluateSuccessPaths:
    def test_valid_verdict_parses(self) -> None:
        judge = _make_judge(
            content='{"risk_level": "medium", "flags": ["camouflaged_injection"], "reasoning": "Authority frame plus caps action."}'
        )
        v = judge.evaluate("any output", func_name="web_fetch", call_id="call-1")
        assert v.succeeded
        assert v.risk_level == "medium"
        assert v.flags == ["camouflaged_injection"]
        assert v.reasoning == "Authority frame plus caps action."
        assert v.call_id == "call-1"
        assert v.judge_model == "test-model"
        assert v.latency_ms >= 0

    def test_verdict_in_markdown_fence(self) -> None:
        judge = _make_judge(
            content='```json\n{"risk_level": "high", "flags": ["prompt_injection"], "reasoning": "Override directive."}\n```'
        )
        v = judge.evaluate("payload", call_id="c1")
        assert v.succeeded
        assert v.risk_level == "high"

    def test_normalizes_critical_to_high(self) -> None:
        judge = _make_judge(content='{"risk_level": "critical", "flags": [], "reasoning": ""}')
        v = judge.evaluate("payload", call_id="c1")
        assert v.succeeded
        assert v.risk_level == "high"

    def test_normalizes_info_to_low(self) -> None:
        judge = _make_judge(content='{"risk_level": "info", "flags": [], "reasoning": ""}')
        v = judge.evaluate("payload", call_id="c1")
        assert v.risk_level == "low"

    def test_empty_output_short_circuits(self) -> None:
        judge = _make_judge(content="UNUSED")
        v = judge.evaluate("", call_id="c1")
        assert v.succeeded
        assert v.risk_level == "none"
        # latency_ms should be 0 since we didn't even call the provider
        assert v.latency_ms == 0


class TestEvaluateFailurePaths:
    def test_empty_completion(self) -> None:
        judge = _make_judge(content="")
        v = judge.evaluate("payload", call_id="c1")
        assert not v.succeeded
        assert v.error == "empty_response"

    def test_unparseable_content(self) -> None:
        judge = _make_judge(content="this is not json")
        v = judge.evaluate("payload", call_id="c1")
        assert not v.succeeded
        assert v.error == "unparseable_verdict"

    def test_invalid_risk_level(self) -> None:
        judge = _make_judge(content='{"risk_level": "bogus", "flags": []}')
        v = judge.evaluate("payload", call_id="c1")
        assert not v.succeeded
        assert v.error == "invalid_risk_level"

    def test_provider_raises(self) -> None:
        judge = _make_judge(raises=RuntimeError("upstream 503"))
        v = judge.evaluate("payload", call_id="c1")
        assert not v.succeeded
        assert v.error.startswith("provider_error:")

    def test_timeout(self) -> None:
        # Provider sleeps 2s but timeout is 1s.
        judge = _make_judge(
            content='{"risk_level":"medium","flags":[],"reasoning":""}', timeout=1.0, delay=2.0
        )
        v = judge.evaluate("payload", call_id="c1")
        assert not v.succeeded
        assert v.error == "timeout"

    def test_cancel_event(self) -> None:
        judge = _make_judge(content='{"risk_level":"medium"}', delay=2.0, timeout=5.0)
        cancel = threading.Event()
        # Fire the cancel from a side thread shortly after evaluate starts.

        def _trigger() -> None:
            time.sleep(0.2)
            cancel.set()

        threading.Thread(target=_trigger, daemon=True).start()
        v = judge.evaluate("payload", call_id="c1", cancel_event=cancel)
        assert not v.succeeded
        assert v.error == "cancelled"


class TestAliasResolution:
    def test_unknown_alias_falls_back_to_session_model(self) -> None:
        # Registry says alias does not exist; judge should fall back.
        registry = MagicMock()
        registry.has_alias.return_value = False
        provider = _make_provider('{"risk_level": "none", "flags": []}')
        config = JudgeConfig(
            output_guard_llm=True,
            output_guard_model="nonexistent-alias",
        )
        judge = OutputGuardJudge(
            config=config,
            session_provider=provider,
            session_client=MagicMock(base_url="http://x", api_key="y"),
            session_model="session-model",
            model_registry=registry,
        )
        assert judge._model == "session-model"
        assert judge._judge_model_alias == ""

    def test_known_alias_resolves(self) -> None:
        registry = MagicMock()
        registry.has_alias.return_value = True
        alias_client = MagicMock(base_url="http://alias", api_key="alias-key")
        alias_provider = MagicMock()
        alias_provider.provider_name = "anthropic"
        registry.resolve.return_value = (alias_client, "claude-haiku-4-5", None)
        registry.get_provider.return_value = alias_provider
        config = JudgeConfig(
            output_guard_llm=True,
            output_guard_model="my-judge",
        )
        judge = OutputGuardJudge(
            config=config,
            session_provider=MagicMock(),
            session_client=MagicMock(base_url="http://session", api_key="s"),
            session_model="session-model",
            model_registry=registry,
        )
        assert judge._model == "claude-haiku-4-5"
        assert judge._judge_model_alias == "my-judge"


class TestExtractJson:
    def test_direct_parse(self) -> None:
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fence(self) -> None:
        assert _extract_json('Pre\n```json\n{"a": 1}\n```\nPost') == {"a": 1}

    def test_first_brace_pair(self) -> None:
        assert _extract_json('prefix {"a": 1} suffix') == {"a": 1}

    def test_unparseable(self) -> None:
        assert _extract_json("no json here") is None
