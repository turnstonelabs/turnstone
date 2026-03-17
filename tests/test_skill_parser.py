"""Tests for turnstone.core.skill_parser."""

from __future__ import annotations

import pytest

from turnstone.core.skill_parser import parse_skill_md, validate_skill_name


class TestParseSkillMd:
    """Parse valid SKILL.md with various field configurations."""

    def test_full_frontmatter(self) -> None:
        raw = """\
---
name: code-review
description: Automated code review skill
author: Test Author
version: 2.0.0
tags: [python, review, quality]
allowed_tools: [read_file, list_directory]
license: MIT
compatibility: ">=0.7"
---

# Code Review

Review code for best practices.
"""
        result = parse_skill_md(raw)
        assert result.name == "code-review"
        assert result.description == "Automated code review skill"
        assert result.author == "Test Author"
        assert result.version == "2.0.0"
        assert result.tags == ["python", "review", "quality"]
        assert result.allowed_tools == ["read_file", "list_directory"]
        assert result.license == "MIT"
        assert result.compatibility == ">=0.7"
        assert "# Code Review" in result.content
        assert result.raw_frontmatter["name"] == "code-review"

    def test_minimal_frontmatter(self) -> None:
        raw = """\
---
name: minimal
---

Just some content.
"""
        result = parse_skill_md(raw)
        assert result.name == "minimal"
        assert result.description == "Just some content."
        assert result.version == "1.0.0"
        assert result.tags == []
        assert result.allowed_tools == []

    def test_missing_name_raises(self) -> None:
        raw = """\
---
description: No name field
---

Content here.
"""
        with pytest.raises(ValueError, match="name is required"):
            parse_skill_md(raw)

    def test_name_too_long_raises(self) -> None:
        raw = f"""\
---
name: {"a" * 65}
---

Content.
"""
        with pytest.raises(ValueError, match="exceeds 64 characters"):
            parse_skill_md(raw)

    def test_name_invalid_chars_raises(self) -> None:
        raw = """\
---
name: Invalid_Name!
---

Content.
"""
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            parse_skill_md(raw)

    def test_single_char_name(self) -> None:
        raw = """\
---
name: x
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.name == "x"

    def test_name_uppercased_normalized(self) -> None:
        raw = """\
---
name: Code-Review
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.name == "code-review"

    def test_description_fallback_from_heading(self) -> None:
        raw = """\
---
name: test-skill
---

# My Awesome Skill

More content here.
"""
        result = parse_skill_md(raw)
        assert result.description == "My Awesome Skill"

    def test_description_fallback_from_text(self) -> None:
        raw = """\
---
name: test-skill
---

This is the first line of content.

And more.
"""
        result = parse_skill_md(raw)
        assert result.description == "This is the first line of content."

    def test_frozen_dataclass(self) -> None:
        result = parse_skill_md("---\nname: frozen-test\n---\nContent.")
        with pytest.raises(AttributeError):
            result.name = "changed"  # type: ignore[misc]


class TestHermesTags:
    """Handle Hermes-format tag nesting."""

    def test_hermes_tags(self) -> None:
        raw = """\
---
name: hermes-skill
metadata:
  hermes:
    tags: [ai, assistant]
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.tags == ["ai", "assistant"]

    def test_anthropic_tags(self) -> None:
        raw = """\
---
name: anthropic-skill
metadata:
  tags: [claude, coding]
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.tags == ["claude", "coding"]

    def test_direct_tags_take_precedence(self) -> None:
        raw = """\
---
name: precedence
tags: [direct]
metadata:
  tags: [nested]
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.tags == ["direct"]


class TestAllowedTools:
    """Verify allowed_tools parsing."""

    def test_list_format(self) -> None:
        raw = """\
---
name: tools-list
allowed_tools: [bash, read_file]
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.allowed_tools == ["bash", "read_file"]

    def test_csv_format(self) -> None:
        raw = """\
---
name: tools-csv
allowed_tools: "bash, read_file, write_file"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.allowed_tools == ["bash", "read_file", "write_file"]

    def test_empty_allowed_tools(self) -> None:
        raw = """\
---
name: no-tools
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.allowed_tools == []


class TestValidateSkillName:
    """Name validation edge cases."""

    def test_valid_names(self) -> None:
        assert validate_skill_name("code-review") is None
        assert validate_skill_name("a") is None
        assert validate_skill_name("my-skill-123") is None
        assert validate_skill_name("x" * 64) is None

    def test_empty_name(self) -> None:
        assert validate_skill_name("") == "name is required"

    def test_too_long(self) -> None:
        err = validate_skill_name("x" * 65)
        assert err is not None
        assert "64 characters" in err

    def test_invalid_characters(self) -> None:
        assert validate_skill_name("has_underscore") is not None
        assert validate_skill_name("HAS-UPPER") is not None
        assert validate_skill_name("has space") is not None
        assert validate_skill_name("-leading-hyphen") is not None
