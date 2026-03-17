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
allowed-tools: [read_file, list_directory]
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
    """Verify allowed-tools parsing (Agent Skills standard hyphenated field)."""

    def test_list_format(self) -> None:
        raw = """\
---
name: tools-list
allowed-tools: [bash, read_file]
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.allowed_tools == ["bash", "read_file"]

    def test_space_delimited_format(self) -> None:
        """Standard format per Agent Skills spec."""
        raw = """\
---
name: tools-space
allowed-tools: "bash read_file write_file"
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

    def test_underscore_key_not_read(self) -> None:
        """allowed_tools (underscore) is not a SKILL.md field — ignored by parser."""
        raw = """\
---
name: legacy-key
allowed_tools: [bash, read_file]
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

    def test_consecutive_hyphens_rejected(self) -> None:
        """Agent Skills spec: consecutive hyphens not allowed."""
        assert validate_skill_name("foo--bar") is not None
        assert "consecutive hyphens" in (validate_skill_name("a--b") or "")
        # Single hyphens are fine
        assert validate_skill_name("foo-bar") is None


# -- Agent Skills Standard Compliance Tests -----------------------------------


class TestStandardAllowedTools:
    """Agent Skills spec: 'allowed-tools' (hyphenated), space-delimited."""

    def test_list_format(self) -> None:
        raw = """\
---
name: standard-tools
allowed-tools: ["Bash(git:*)", "Bash(jq:*)", "Read"]
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.allowed_tools == ["Bash(git:*)", "Bash(jq:*)", "Read"]

    def test_space_delimited(self) -> None:
        """Standard format: space-delimited string."""
        raw = """\
---
name: space-tools
allowed-tools: "Bash(git:*) Bash(jq:*) Read"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.allowed_tools == ["Bash(git:*)", "Bash(jq:*)", "Read"]

    def test_mixed_space_comma_delimiters(self) -> None:
        raw = """\
---
name: mixed-delim
allowed-tools: "Read, Write Bash"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.allowed_tools == ["Read", "Write", "Bash"]


class TestStandardMetadataNesting:
    """Standard puts author/version under metadata map."""

    def test_metadata_author(self) -> None:
        raw = """\
---
name: nested-author
description: Test skill
metadata:
  author: example-org
  version: "2.0"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.author == "example-org"
        assert result.version == "2.0"

    def test_top_level_takes_precedence(self) -> None:
        raw = """\
---
name: precedence
description: Test skill
author: top-level
version: 1.0.0
metadata:
  author: nested
  version: "2.0"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.author == "top-level"
        assert result.version == "1.0.0"

    def test_metadata_version_only(self) -> None:
        raw = """\
---
name: version-only
description: Test
metadata:
  version: "3.5.1"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.version == "3.5.1"
        assert result.author == ""

    def test_null_author_uses_default(self) -> None:
        """YAML null/bare key must not produce the string 'None'."""
        raw = """\
---
name: null-author
description: Test
author:
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.author == ""
        assert result.version == "1.0.0"

    def test_null_version_uses_default(self) -> None:
        raw = """\
---
name: null-version
description: Test
version:
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.version == "1.0.0"


class TestStandardFieldLengths:
    """Spec caps: description <= 1024, compatibility <= 500."""

    def test_description_truncated_at_1024(self) -> None:
        long_desc = "x" * 1200
        raw = f"""\
---
name: long-desc
description: "{long_desc}"
---

Content.
"""
        result = parse_skill_md(raw)
        assert len(result.description) == 1024

    def test_compatibility_truncated_at_500(self) -> None:
        long_compat = "y" * 600
        raw = f"""\
---
name: long-compat
description: Short
compatibility: "{long_compat}"
---

Content.
"""
        result = parse_skill_md(raw)
        assert len(result.compatibility) == 500

    def test_short_fields_unchanged(self) -> None:
        raw = """\
---
name: short
description: Brief
compatibility: Requires git
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.description == "Brief"
        assert result.compatibility == "Requires git"


class TestLenientMode:
    """Lenient parsing for cross-client skill ingestion."""

    def test_invalid_name_sanitized(self) -> None:
        raw = """\
---
name: Invalid_Name!
description: A test skill
---

Content.
"""
        result = parse_skill_md(raw, lenient=True)
        assert result is not None
        assert result.name == "invalidname"

    def test_unsalvageable_name_returns_none(self) -> None:
        raw = """\
---
name: "!!!"
description: A test skill
---

Content.
"""
        assert parse_skill_md(raw, lenient=True) is None

    def test_missing_description_returns_none(self) -> None:
        raw = """\
---
name: no-desc
---
"""
        assert parse_skill_md(raw, lenient=True) is None

    def test_broken_yaml_returns_none(self) -> None:
        raw = """\
---
name: [broken: yaml: {{{
---

Content.
"""
        assert parse_skill_md(raw, lenient=True) is None

    def test_malformed_yaml_colon_in_description_recovers(self) -> None:
        """Standard recommends retrying unquoted colon values."""
        raw = """\
---
name: colon-desc
description: Use this skill when: the user asks about PDFs
---

Content.
"""
        result = parse_skill_md(raw, lenient=True)
        # The frontmatter library may parse this fine, but if not,
        # the retry mechanism should recover.
        assert result is not None
        assert result.name == "colon-desc"
        assert "PDF" in result.description

    def test_strict_mode_still_raises(self) -> None:
        """Default strict mode unchanged."""
        raw = """\
---
name: Invalid_Name!
description: A test skill
---

Content.
"""
        with pytest.raises(ValueError):
            parse_skill_md(raw)

    def test_consecutive_hyphens_lenient(self) -> None:
        raw = """\
---
name: foo--bar
description: A test skill
---

Content.
"""
        result = parse_skill_md(raw, lenient=True)
        assert result is not None
        assert "--" not in result.name
