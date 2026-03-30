"""Tests for pure helper functions in mcp_cluster_ops.server."""

from __future__ import annotations

from turnstone.sdk import TurnResult

from mcp_cluster_ops.server import (
    _clamp_timeout,
    _exec_prompt,
    _extract_output,
    _format_node_result,
    _truncate,
    _validate_command,
)

# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_empty_string(self):
        assert _truncate("", 100) == ""

    def test_under_limit(self):
        assert _truncate("hello", 100) == "hello"

    def test_at_limit(self):
        text = "x" * 50
        assert _truncate(text, 50) == text

    def test_over_limit(self):
        text = "x" * 200
        result = _truncate(text, 50)
        assert result.startswith("x" * 50)
        assert "truncated" in result
        assert "150 bytes omitted" in result

    def test_unicode_boundary(self):
        # U+00E9 (é) is 2 bytes in UTF-8 (0xC3 0xA9), so 5 chars = 10 bytes
        text = "\u00e9\u00e9\u00e9\u00e9\u00e9"
        result = _truncate(text, 5)
        # Should not crash, should truncate cleanly
        assert "truncated" in result

    def test_zero_disables(self):
        text = "x" * 10000
        assert _truncate(text, 0) == text

    def test_custom_max(self):
        text = "abcdefghij"  # 10 bytes
        result = _truncate(text, 5)
        assert result.startswith("abcde")
        assert "truncated" in result


# ---------------------------------------------------------------------------
# _extract_output
# ---------------------------------------------------------------------------


class TestExtractOutput:
    def test_bash_result_preferred(self):
        r = TurnResult(
            content_parts=["agent said something"],
            tool_results=[("bash", "raw output")],
        )
        assert _extract_output(r) == "raw output"

    def test_multiple_bash_results_joined(self):
        r = TurnResult(
            tool_results=[("bash", "line1"), ("bash", "line2")],
        )
        assert _extract_output(r) == "line1\nline2"

    def test_content_fallback(self):
        r = TurnResult(
            content_parts=["agent response"],
            tool_results=[("read_file", "file contents")],
        )
        assert _extract_output(r) == "agent response"

    def test_any_tool_fallback(self):
        r = TurnResult(
            tool_results=[("read_file", "file contents")],
        )
        assert _extract_output(r) == "file contents"

    def test_empty_result(self):
        r = TurnResult()
        assert _extract_output(r) == ""

    def test_bash_preferred_over_content(self):
        r = TurnResult(
            content_parts=["I ran the command"],
            tool_results=[("read_file", "data"), ("bash", "output")],
        )
        assert _extract_output(r) == "output"


# ---------------------------------------------------------------------------
# _exec_prompt
# ---------------------------------------------------------------------------


class TestExecPrompt:
    def test_contains_command(self):
        result = _exec_prompt("ls -la /tmp")
        assert "ls -la /tmp" in result

    def test_suppression_instruction(self):
        result = _exec_prompt("echo hello")
        assert "Do NOT repeat" in result
        assert "ok" in result.lower() or "failed" in result.lower()


# ---------------------------------------------------------------------------
# _format_node_result
# ---------------------------------------------------------------------------


class TestFormatNodeResult:
    def test_success(self):
        r = TurnResult(tool_results=[("bash", "output data")])
        fmt = _format_node_result("node-1", r, 8192)
        assert fmt["node"] == "node-1"
        assert fmt["ok"] is True
        assert fmt["output"] == "output data"
        assert "timed_out" not in fmt

    def test_timeout(self):
        r = TurnResult(timed_out=True)
        fmt = _format_node_result("node-1", r, 8192)
        assert fmt["ok"] is False
        assert fmt["timed_out"] is True

    def test_error(self):
        r = TurnResult(errors=["connection refused"])
        fmt = _format_node_result("node-1", r, 8192)
        assert fmt["ok"] is False
        assert fmt["error"] == "connection refused"

    def test_truncation_applied(self):
        r = TurnResult(tool_results=[("bash", "x" * 200)])
        fmt = _format_node_result("node-1", r, 50)
        assert "truncated" in fmt["output"]

    def test_unlimited_output(self):
        big = "x" * 100000
        r = TurnResult(tool_results=[("bash", big)])
        fmt = _format_node_result("node-1", r, 0)
        assert fmt["output"] == big


# ---------------------------------------------------------------------------
# _validate_command
# ---------------------------------------------------------------------------


class TestValidateCommand:
    def test_valid(self):
        assert _validate_command("ls -la") is None

    def test_empty(self):
        assert _validate_command("") is not None

    def test_whitespace_only(self):
        assert _validate_command("   ") is not None

    def test_too_long(self):
        err = _validate_command("x" * 100000)
        assert err is not None
        assert "too long" in err


# ---------------------------------------------------------------------------
# _clamp_timeout
# ---------------------------------------------------------------------------


class TestClampTimeout:
    def test_normal(self):
        assert _clamp_timeout(60) == 60.0

    def test_too_low(self):
        assert _clamp_timeout(1) == 5.0

    def test_too_high(self):
        assert _clamp_timeout(99999) == 3600.0

    def test_negative(self):
        assert _clamp_timeout(-1) == 5.0
