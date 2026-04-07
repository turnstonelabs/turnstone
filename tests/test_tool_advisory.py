"""Tests for turnstone.core.tool_advisory."""

from __future__ import annotations

from turnstone.core.output_guard import OutputAssessment
from turnstone.core.tool_advisory import (
    GuardAdvisory,
    UserInterjection,
    parse_priority,
    wrap_tool_result,
)


class TestWrapToolResult:
    """wrap_tool_result() wraps only when advisories are present."""

    def test_no_advisories_passthrough(self) -> None:
        assert wrap_tool_result("hello world") == "hello world"

    def test_none_advisories_passthrough(self) -> None:
        assert wrap_tool_result("hello world", None) == "hello world"

    def test_empty_list_passthrough(self) -> None:
        assert wrap_tool_result("hello world", []) == "hello world"

    def test_single_advisory_wraps(self) -> None:
        adv = UserInterjection(message="check auth too", priority="notice")
        result = wrap_tool_result("file contents here", [adv])
        assert "<tool_output>" in result
        assert "file contents here" in result
        assert "<system-reminder>" in result
        assert "check auth too" in result

    def test_multiple_advisories(self) -> None:
        guard = GuardAdvisory(
            assessment=OutputAssessment(
                flags=["credential_leak"],
                risk_level="high",
                annotations=["API key detected"],
                sanitized="sk-[REDACTED:api_key]",
            ),
            func_name="read_file",
        )
        user = UserInterjection(message="also check .env", priority="notice")
        result = wrap_tool_result("sk-proj-abc123", [guard, user])
        # Both advisories rendered as separate system-reminder blocks
        assert result.count("<system-reminder>") == 2
        assert "credential_leak" in result
        assert "also check .env" in result

    def test_tool_output_tags_wrap_content(self) -> None:
        adv = UserInterjection(message="test", priority="notice")
        result = wrap_tool_result("raw output", [adv])
        # Content should be inside tool_output tags
        start = result.index("<tool_output>")
        end = result.index("</tool_output>")
        inner = result[start : end + len("</tool_output>")]
        assert "raw output" in inner


class TestGuardAdvisory:
    """GuardAdvisory renders output guard findings for model consumption."""

    def test_advisory_type(self) -> None:
        adv = GuardAdvisory(
            assessment=OutputAssessment(flags=["prompt_injection"], risk_level="high"),
            func_name="bash",
        )
        assert adv.advisory_type == "output_guard"

    def test_render_flags_and_risk(self) -> None:
        adv = GuardAdvisory(
            assessment=OutputAssessment(
                flags=["prompt_injection"],
                risk_level="high",
                annotations=["Override phrase detected"],
            ),
            func_name="bash",
        )
        text = adv.render()
        assert "prompt_injection" in text
        assert "HIGH" in text
        assert "Override phrase detected" in text

    def test_render_redaction_notice(self) -> None:
        adv = GuardAdvisory(
            assessment=OutputAssessment(
                flags=["credential_leak"],
                risk_level="high",
                annotations=["API key found"],
                sanitized="[REDACTED:api_key]",
            ),
            func_name="read_file",
        )
        text = adv.render()
        assert "redacted" in text.lower()
        assert "Do not attempt to reconstruct" in text

    def test_render_no_redaction_when_no_sanitized(self) -> None:
        adv = GuardAdvisory(
            assessment=OutputAssessment(
                flags=["info_disclosure"],
                risk_level="low",
                annotations=["Private IP found"],
            ),
            func_name="bash",
        )
        text = adv.render()
        assert "reconstruct" not in text


class TestUserInterjection:
    """UserInterjection renders queued user messages with priority framing."""

    def test_advisory_type(self) -> None:
        adv = UserInterjection(message="hello", priority="notice")
        assert adv.advisory_type == "user_interjection"

    def test_notice_priority(self) -> None:
        adv = UserInterjection(message="also check logs", priority="notice")
        text = adv.render()
        assert "also check logs" in text
        assert "Incorporate if relevant" in text
        assert "MUST" not in text

    def test_important_priority(self) -> None:
        adv = UserInterjection(message="stop and check auth", priority="important")
        text = adv.render()
        assert "stop and check auth" in text
        assert "MUST address" in text

    def test_default_priority_is_notice(self) -> None:
        adv = UserInterjection(message="test")
        assert adv.priority == "notice"


class TestParsePriority:
    """parse_priority() extracts !!! prefix as priority signal."""

    def test_no_prefix(self) -> None:
        text, priority = parse_priority("hello world")
        assert text == "hello world"
        assert priority == "notice"

    def test_triple_bang_important(self) -> None:
        text, priority = parse_priority("!!!check the auth endpoint")
        assert text == "check the auth endpoint"
        assert priority == "important"

    def test_triple_bang_with_space(self) -> None:
        text, priority = parse_priority("!!! check the auth endpoint")
        assert text == "check the auth endpoint"
        assert priority == "important"

    def test_single_bang_not_priority(self) -> None:
        text, priority = parse_priority("!important message")
        assert text == "!important message"
        assert priority == "notice"

    def test_double_bang_not_priority(self) -> None:
        text, priority = parse_priority("!!not quite")
        assert text == "!!not quite"
        assert priority == "notice"

    def test_empty_after_prefix(self) -> None:
        text, priority = parse_priority("!!!")
        assert text == ""
        assert priority == "important"
