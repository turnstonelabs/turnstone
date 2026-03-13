"""Tests for the IntentJudge LLM evaluation engine."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from turnstone.core.judge import IntentJudge, IntentVerdict, JudgeConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_provider(
    response_content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    *,
    side_effect: Exception | None = None,
) -> MagicMock:
    """Create a mock LLM provider that returns a fixed response."""
    provider = MagicMock()
    caps = MagicMock()
    caps.context_window = 100_000
    caps.max_output_tokens = 4096
    provider.get_capabilities.return_value = caps

    result = MagicMock()
    result.content = response_content
    result.tool_calls = tool_calls
    result.finish_reason = "stop"
    result.usage = None

    if side_effect:
        provider.create_completion.side_effect = side_effect
    else:
        provider.create_completion.return_value = result

    provider.convert_tools.side_effect = lambda tools, **kw: tools

    return provider


def _make_judge(
    provider: MagicMock | None = None,
    *,
    confidence_threshold: float = 0.7,
    read_only_tools: bool = True,
    timeout: float = 60.0,
) -> IntentJudge:
    """Create a judge with a mock provider."""
    if provider is None:
        provider = _make_mock_provider()

    config = JudgeConfig(
        enabled=True,
        confidence_threshold=confidence_threshold,
        read_only_tools=read_only_tools,
        timeout=timeout,
    )
    client = MagicMock()
    return IntentJudge(
        config=config,
        session_provider=provider,
        session_client=client,
        session_model="test-model",
        context_window=100_000,
    )


def _make_item(**overrides: Any) -> dict[str, Any]:
    """Create a minimal tool call item."""
    defaults = {
        "func_name": "bash",
        "func_args": {"command": "echo hello"},
        "approval_label": "bash",
        "call_id": "tc_001",
    }
    defaults.update(overrides)
    return defaults


def _good_verdict_json(**overrides: Any) -> str:
    """Return a well-formed JSON verdict string."""
    verdict = {
        "intent_summary": "Echo a greeting",
        "risk_level": "low",
        "confidence": 0.95,
        "recommendation": "approve",
        "reasoning": "Simple echo command with no side effects.",
        "evidence": ["The command only prints text to stdout."],
    }
    verdict.update(overrides)
    return json.dumps(verdict)


# ---------------------------------------------------------------------------
# JSON parsing strategies
# ---------------------------------------------------------------------------


class TestVerdictParsing:
    def test_valid_json_direct(self):
        """Provider returns pure JSON — parsed via strategy 1."""
        content = _good_verdict_json()
        provider = _make_mock_provider(response_content=content)
        judge = _make_judge(provider)

        callback_results: list[IntentVerdict] = []
        heuristics = judge.evaluate(
            [_make_item()],
            [{"role": "user", "content": "Run echo hello"}],
            callback_results.append,
        )
        # Wait for daemon thread
        time.sleep(0.5)

        assert len(heuristics) == 1
        assert heuristics[0].tier == "heuristic"

    def test_markdown_code_block(self):
        """Provider wraps verdict in ```json ... ``` — strategy 2."""
        content = "Here is my verdict:\n```json\n" + _good_verdict_json() + "\n```"
        judge = _make_judge(_make_mock_provider(response_content=content))

        verdict = judge._parse_verdict(content, "bash", "tc_001", 50)
        assert verdict is not None
        assert verdict.risk_level == "low"
        assert verdict.recommendation == "approve"
        assert verdict.tier == "llm"

    def test_brace_counting_fallback(self):
        """Provider returns verdict embedded in prose — strategy 3."""
        content = (
            "After careful analysis, my verdict is: "
            + _good_verdict_json()
            + " That concludes my review."
        )
        judge = _make_judge()
        verdict = judge._parse_verdict(content, "bash", "tc_001", 50)
        assert verdict is not None
        assert verdict.risk_level == "low"

    def test_regex_field_extraction(self):
        """Broken JSON but fields extractable via regex — strategy 4."""
        content = (
            "Here is my analysis:\n"
            '"intent_summary": "Echo command",\n'
            '"risk_level": "low",\n'
            '"confidence": 0.9,\n'
            '"recommendation": "approve",\n'
            '"reasoning": "Safe command"\n'
        )
        judge = _make_judge()
        verdict = judge._parse_verdict(content, "bash", "tc_001", 50)
        assert verdict is not None
        assert verdict.risk_level == "low"
        assert verdict.confidence == 0.9
        assert verdict.recommendation == "approve"

    def test_unparseable_returns_none(self):
        """Provider returns completely unparseable text."""
        judge = _make_judge()
        verdict = judge._parse_verdict("I cannot evaluate this.", "bash", "tc_001", 50)
        assert verdict is None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_provider_exception_returns_none(self):
        """Provider raises exception — caught, returns None."""
        provider = _make_mock_provider(side_effect=RuntimeError("API error"))
        judge = _make_judge(provider)

        result = judge._evaluate_single(
            _make_item(),
            [{"role": "user", "content": "test"}],
            MagicMock(),
        )
        assert result is None

    def test_provider_error_heuristic_still_returned(self):
        """When LLM fails, heuristic verdicts are still returned from evaluate()."""
        provider = _make_mock_provider(side_effect=RuntimeError("API down"))
        judge = _make_judge(provider)

        callback_results: list[IntentVerdict] = []
        heuristics = judge.evaluate(
            [_make_item()],
            [{"role": "user", "content": "test"}],
            callback_results.append,
        )
        time.sleep(0.5)

        assert len(heuristics) == 1
        assert heuristics[0].tier == "heuristic"
        # Callback should not have been invoked (LLM failed)
        assert len(callback_results) == 0

    def test_empty_content_returns_none(self):
        """Provider returns empty content, no tool calls."""
        provider = _make_mock_provider(response_content="")
        result_mock = provider.create_completion.return_value
        result_mock.tool_calls = None
        result_mock.content = ""

        judge = _make_judge(provider)
        result = judge._evaluate_single(
            _make_item(),
            [{"role": "user", "content": "test"}],
            MagicMock(),
        )
        assert result is None


# ---------------------------------------------------------------------------
# Multi-turn tool use
# ---------------------------------------------------------------------------


class TestMultiTurnToolUse:
    def test_tool_call_then_verdict(self):
        """Provider requests read_file, then returns verdict."""
        provider = MagicMock()
        caps = MagicMock()
        caps.context_window = 100_000
        caps.max_output_tokens = 4096
        provider.get_capabilities.return_value = caps
        provider.convert_tools.side_effect = lambda tools, **kw: tools

        # Turn 1: tool call
        turn1 = MagicMock()
        turn1.content = ""
        turn1.tool_calls = [
            {
                "id": "tc_judge_1",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "/nonexistent/file.txt"}),
                },
            }
        ]

        # Turn 2: verdict
        turn2 = MagicMock()
        turn2.content = _good_verdict_json()
        turn2.tool_calls = None

        provider.create_completion.side_effect = [turn1, turn2]

        judge = _make_judge(provider)
        verdict = judge._evaluate_single(
            _make_item(),
            [{"role": "user", "content": "test"}],
            MagicMock(),
        )
        assert verdict is not None
        assert verdict.tier == "llm"
        assert provider.create_completion.call_count == 2

    def test_max_turns_reached(self):
        """Provider keeps requesting tools — stops at _JUDGE_MAX_TURNS."""
        provider = MagicMock()
        caps = MagicMock()
        caps.context_window = 100_000
        caps.max_output_tokens = 4096
        provider.get_capabilities.return_value = caps
        provider.convert_tools.side_effect = lambda tools, **kw: tools

        # Every turn returns a tool call
        tool_result = MagicMock()
        tool_result.content = ""
        tool_result.tool_calls = [
            {
                "id": "tc_loop",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "/tmp/x"}),
                },
            }
        ]

        # Last turn (no tools param) returns text content
        final = MagicMock()
        final.content = _good_verdict_json()
        final.tool_calls = None

        # Turns 0-3: tool_call; turn 4 (last, tools=None): final verdict
        provider.create_completion.side_effect = [
            tool_result,
            tool_result,
            tool_result,
            tool_result,
            final,
        ]

        judge = _make_judge(provider)
        judge._evaluate_single(
            _make_item(),
            [{"role": "user", "content": "test"}],
            MagicMock(),
        )
        # Should have called create_completion exactly _JUDGE_MAX_TURNS times
        assert provider.create_completion.call_count == 5


# ---------------------------------------------------------------------------
# Context preparation
# ---------------------------------------------------------------------------


class TestContextPreparation:
    def test_context_truncation(self):
        """Long conversation history gets truncated to budget."""
        judge = _make_judge()

        # Create a large message history
        messages = [{"role": "user", "content": "x" * 10000} for _ in range(100)]

        result = judge._prepare_context(_make_item(), messages)

        # Should have system message + some truncated history + user message
        assert result[0]["role"] == "system"
        assert result[-1]["role"] == "user"
        assert "pending human approval" in result[-1]["content"]
        # Should be fewer messages than the original 100
        assert len(result) < 102  # system + 100 + user


# ---------------------------------------------------------------------------
# Confidence arbitration
# ---------------------------------------------------------------------------


class TestConfidenceArbitration:
    def test_llm_higher_confidence_triggers_callback(self):
        """LLM confidence > heuristic confidence — callback invoked."""
        provider = _make_mock_provider(response_content=_good_verdict_json(confidence=0.95))
        judge = _make_judge(provider)

        callback_results: list[IntentVerdict] = []
        # bash "echo hello" → heuristic confidence 0.85 (low/bash-read-only)
        heuristics = judge.evaluate(
            [_make_item()],
            [{"role": "user", "content": "Run echo hello"}],
            callback_results.append,
        )
        time.sleep(0.5)

        assert len(heuristics) == 1
        assert heuristics[0].confidence == 0.85
        assert len(callback_results) == 1
        assert callback_results[0].tier == "llm"
        assert callback_results[0].confidence == 0.95

    def test_llm_lower_confidence_no_callback(self):
        """LLM confidence < heuristic confidence — no callback."""
        provider = _make_mock_provider(response_content=_good_verdict_json(confidence=0.5))
        judge = _make_judge(provider)

        callback_results: list[IntentVerdict] = []
        # bash "echo hello" → heuristic confidence 0.85
        heuristics = judge.evaluate(
            [_make_item()],
            [{"role": "user", "content": "Run echo hello"}],
            callback_results.append,
        )
        time.sleep(0.5)

        assert len(heuristics) == 1
        # LLM confidence (0.5) < heuristic (0.85), so no callback
        assert len(callback_results) == 0


# ---------------------------------------------------------------------------
# Path blocking
# ---------------------------------------------------------------------------


class TestPathBlocking:
    def test_etc_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/etc/passwd")) is True

    def test_root_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/root/.bashrc")) is True

    def test_proc_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/proc/1/status")) is True

    def test_sys_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/sys/class/net")) is True

    def test_dev_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/dev/sda")) is True

    def test_ssh_part_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/home/user/.ssh/id_rsa")) is True

    def test_gnupg_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/home/user/.gnupg/private-keys")) is True

    def test_aws_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/home/user/.aws/credentials")) is True

    def test_config_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/home/user/.config/secret")) is True

    def test_pem_suffix_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/tmp/server.pem")) is True

    def test_key_suffix_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/tmp/private.key")) is True

    def test_p12_suffix_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/tmp/cert.p12")) is True

    def test_pfx_suffix_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/tmp/cert.pfx")) is True

    def test_safe_path_not_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/tmp/test.txt")) is False

    def test_project_path_not_blocked(self):
        assert IntentJudge._is_path_blocked(Path("/home/user/project/main.py")) is False


# ---------------------------------------------------------------------------
# Read-only tool execution
# ---------------------------------------------------------------------------


class TestReadOnlyToolExecution:
    def test_read_file_success(self, tmp_path):
        test_file = tmp_path / "hello.txt"
        test_file.write_text("Hello, world!")
        result = IntentJudge._exec_read_only_tool("read_file", {"path": str(test_file)})
        assert result == "Hello, world!"

    def test_read_file_not_found(self):
        result = IntentJudge._exec_read_only_tool("read_file", {"path": "/nonexistent/file.txt"})
        assert "Error" in result
        assert "not found" in result

    def test_read_file_blocked_path(self):
        result = IntentJudge._exec_read_only_tool("read_file", {"path": "/etc/shadow"})
        assert "access denied" in result

    def test_read_file_truncation(self, tmp_path):
        test_file = tmp_path / "big.txt"
        test_file.write_text("x" * 50_000)
        result = IntentJudge._exec_read_only_tool("read_file", {"path": str(test_file)})
        assert "truncated" in result
        assert len(result) < 50_000

    def test_list_directory_success(self, tmp_path):
        (tmp_path / "file_a.txt").touch()
        (tmp_path / "dir_b").mkdir()
        result = IntentJudge._exec_read_only_tool("list_directory", {"path": str(tmp_path)})
        assert "dir_b/" in result
        assert "file_a.txt" in result

    def test_list_directory_not_found(self):
        result = IntentJudge._exec_read_only_tool("list_directory", {"path": "/nonexistent/dir"})
        assert "Error" in result
        assert "not found" in result

    def test_list_directory_blocked(self):
        result = IntentJudge._exec_read_only_tool("list_directory", {"path": "/etc/ssl"})
        assert "access denied" in result

    def test_unknown_tool(self):
        result = IntentJudge._exec_read_only_tool("write_file", {"path": "/tmp/x"})
        assert "unknown tool" in result


# ---------------------------------------------------------------------------
# Verdict normalization
# ---------------------------------------------------------------------------


class TestVerdictNormalization:
    def test_invalid_risk_level_normalized(self):
        content = _good_verdict_json(risk_level="extreme")
        judge = _make_judge()
        verdict = judge._parse_verdict(content, "bash", "tc_001", 50)
        assert verdict is not None
        assert verdict.risk_level == "medium"  # default

    def test_invalid_recommendation_normalized(self):
        content = _good_verdict_json(recommendation="maybe")
        judge = _make_judge()
        verdict = judge._parse_verdict(content, "bash", "tc_001", 50)
        assert verdict is not None
        assert verdict.recommendation == "review"  # default

    def test_confidence_clamped_above_1(self):
        content = _good_verdict_json(confidence=1.5)
        judge = _make_judge()
        verdict = judge._parse_verdict(content, "bash", "tc_001", 50)
        assert verdict is not None
        assert verdict.confidence == 1.0

    def test_confidence_clamped_below_0(self):
        content = _good_verdict_json(confidence=-0.3)
        judge = _make_judge()
        verdict = judge._parse_verdict(content, "bash", "tc_001", 50)
        assert verdict is not None
        assert verdict.confidence == 0.0

    def test_evidence_string_wrapped_in_list(self):
        content = _good_verdict_json(evidence="single evidence string")
        judge = _make_judge()
        verdict = judge._parse_verdict(content, "bash", "tc_001", 50)
        assert verdict is not None
        assert verdict.evidence == ["single evidence string"]
