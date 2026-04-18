"""Tests for turnstone.channels._formatter."""

from __future__ import annotations

from turnstone.channels._formatter import (
    chunk_message,
    format_approval_request,
    format_verdict,
    truncate,
)

# ---------------------------------------------------------------------------
# chunk_message
# ---------------------------------------------------------------------------


class TestChunkMessage:
    def test_empty_string(self) -> None:
        assert chunk_message("") == [""]

    def test_under_limit(self) -> None:
        assert chunk_message("short text", max_length=100) == ["short text"]

    def test_exactly_at_limit(self) -> None:
        text = "a" * 50
        assert chunk_message(text, max_length=50) == [text]

    def test_splits_at_newline(self) -> None:
        text = "line one\nline two\nline three"
        chunks = chunk_message(text, max_length=18)
        assert len(chunks) >= 2
        # The split should happen at a newline boundary within the text.
        # Reassembled chunks (with newline separators) should cover all content.
        rejoined = "\n".join(chunks)
        assert "line one" in rejoined
        assert "line three" in rejoined

    def test_splits_at_word_boundary(self) -> None:
        text = "word1 word2 word3 word4"
        chunks = chunk_message(text, max_length=12)
        assert len(chunks) >= 2
        # No chunk should start with a space (lstrip handles newlines).
        for chunk in chunks:
            assert not chunk.startswith("\n")

    def test_hard_splits(self) -> None:
        text = "a" * 30
        chunks = chunk_message(text, max_length=10)
        assert len(chunks) == 3
        assert "".join(chunks) == text

    def test_code_block_spanning_boundary(self) -> None:
        text = "before\n```\ncode line 1\ncode line 2\ncode line 3\n```\nafter"
        chunks = chunk_message(text, max_length=30)
        assert len(chunks) >= 2
        # If a chunk opens a code block without closing it, the chunker
        # should close it and reopen in the next chunk.
        for chunk in chunks:
            fence_count = chunk.count("```")
            assert fence_count % 2 == 0, f"Unmatched code fence in chunk: {chunk!r}"

    def test_multiple_code_blocks(self) -> None:
        text = "```\nblock1\n```\ntext\n```\nblock2\n```"
        chunks = chunk_message(text, max_length=20)
        for chunk in chunks:
            fence_count = chunk.count("```")
            assert fence_count % 2 == 0, f"Unmatched code fence in chunk: {chunk!r}"

    def test_custom_max_length(self) -> None:
        text = "hello world"
        chunks = chunk_message(text, max_length=5)
        assert len(chunks) >= 2
        assert chunks[0] == "hello"

    def test_very_long_single_line(self) -> None:
        text = "x" * 5000
        chunks = chunk_message(text, max_length=2000)
        assert len(chunks) == 3
        total = "".join(chunks)
        assert total == text


# ---------------------------------------------------------------------------
# format_approval_request
# ---------------------------------------------------------------------------


class TestFormatApprovalRequest:
    def test_single_tool(self) -> None:
        items = [{"function": {"name": "read_file", "arguments": "/etc/hosts"}}]
        result = format_approval_request(items)
        assert "Tool approval required" in result
        assert "`read_file`" in result

    def test_multiple_tools(self) -> None:
        items = [
            {"function": {"name": "tool_a", "arguments": "arg1"}},
            {"function": {"name": "tool_b", "arguments": "arg2"}},
        ]
        result = format_approval_request(items)
        assert "`tool_a`" in result
        assert "`tool_b`" in result

    def test_long_arguments_truncated(self) -> None:
        long_args = "x" * 500
        items = [{"function": {"name": "fn", "arguments": long_args}}]
        result = format_approval_request(items)
        # The result should be shorter than the original args.
        assert len(result) < 500

    def test_server_sse_format(self) -> None:
        """Items from the server SSE use func_name/preview, not function.name."""
        items = [
            {
                "call_id": "c1",
                "func_name": "bash",
                "preview": "ls -la",
                "header": "Execute: ls -la",
                "needs_approval": True,
            }
        ]
        result = format_approval_request(items)
        assert "`bash`" in result
        assert "Execute: ls -la" in result

    def test_server_sse_format_no_header(self) -> None:
        items = [{"func_name": "read_file", "preview": "/etc/hosts"}]
        result = format_approval_request(items)
        assert "`read_file`" in result
        assert "/etc/hosts" in result


# ---------------------------------------------------------------------------
# format_verdict
# ---------------------------------------------------------------------------


class TestFormatVerdict:
    def test_low_risk(self) -> None:
        verdict = {
            "risk_level": "low",
            "recommendation": "allow",
            "confidence": 0.95,
            "intent_summary": "Reading a config file",
            "tier": "heuristic",
        }
        result = format_verdict(verdict)
        assert "HEURISTIC" in result
        assert "LOW" in result
        assert "95%" in result
        assert "allow" in result
        assert "_Reading a config file_" in result
        # Green circle emoji
        assert "\U0001f7e2" in result

    def test_high_risk(self) -> None:
        verdict = {
            "risk_level": "high",
            "recommendation": "deny",
            "confidence": 0.8,
        }
        result = format_verdict(verdict)
        assert "HIGH" in result
        assert "80%" in result
        assert "deny" in result
        # Red circle emoji
        assert "\U0001f534" in result

    def test_critical_risk(self) -> None:
        verdict = {"risk_level": "critical", "confidence": 0.99}
        result = format_verdict(verdict)
        assert "CRITICAL" in result
        assert "\u26d4" in result

    def test_medium_risk_default(self) -> None:
        """Empty risk_level defaults to MEDIUM."""
        result = format_verdict({})
        assert "MEDIUM" in result
        assert "50%" in result
        assert "review" in result

    def test_no_summary_omits_line(self) -> None:
        verdict = {"risk_level": "low", "confidence": 0.7}
        result = format_verdict(verdict)
        # Should be a single line (no summary italic line).
        assert "\n" not in result

    def test_with_summary(self) -> None:
        verdict = {"risk_level": "low", "intent_summary": "Safe operation"}
        result = format_verdict(verdict)
        lines = result.split("\n")
        assert len(lines) == 2
        assert "_Safe operation_" in lines[1]

    def test_tier_label(self) -> None:
        verdict = {"tier": "llm", "risk_level": "medium"}
        result = format_verdict(verdict)
        assert "LLM " in result

    def test_no_tier_no_label(self) -> None:
        verdict = {"risk_level": "low"}
        result = format_verdict(verdict)
        assert "Risk: LOW" in result
        # No double space or extra label prefix.
        assert "** " not in result or "**Risk:" in result


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert truncate("hello", max_length=200) == "hello"

    def test_long_text_truncated(self) -> None:
        text = "a" * 300
        result = truncate(text, max_length=200)
        assert len(result) == 200
        assert result.endswith("\u2026")

    def test_exactly_at_limit(self) -> None:
        text = "b" * 200
        assert truncate(text, max_length=200) == text
