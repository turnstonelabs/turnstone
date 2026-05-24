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

    def test_nested_metadata_tags(self) -> None:
        raw = """\
---
name: nested-tags-skill
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


class TestPaths:
    """SKILL.md spec ``paths:`` — glob patterns gating autoload."""

    def test_list_format(self) -> None:
        raw = """\
---
name: paths-list
paths: ["**/*.py", "packages/api/**"]
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.paths == ["**/*.py", "packages/api/**"]

    def test_comma_separated_string(self) -> None:
        """Spec accepts comma-separated string OR YAML list."""
        raw = """\
---
name: paths-csv
paths: "**/*.py, packages/api/**"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.paths == ["**/*.py", "packages/api/**"]

    def test_empty_paths(self) -> None:
        raw = """\
---
name: no-paths
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.paths == []

    def test_paths_with_full_frontmatter(self) -> None:
        """``paths`` round-trips alongside the other spec fields."""
        raw = """\
---
name: full
description: Has every field
allowed-tools: [bash]
paths: ["**/*.md"]
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.allowed_tools == ["bash"]
        assert result.paths == ["**/*.md"]


class TestWhenToUse:
    """SKILL.md spec ``when_to_use:`` — appended to description at parse time."""

    def test_appended_to_description(self) -> None:
        raw = """\
---
name: with-when
description: Base description.
when_to_use: when the user asks about X
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.when_to_use == "when the user asks about X"
        assert result.description == (
            "Base description.\n\nWhen to use: when the user asks about X"
        )

    def test_when_to_use_appends_to_body_fallback_description(self) -> None:
        """Without an explicit ``description``, the parser falls back to the
        first body line, then ``when_to_use`` appends to that.  Documents
        the layering — when_to_use is *additional* trigger context, never
        a replacement for description."""
        raw = """\
---
name: when-only
when_to_use: trigger phrase
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.when_to_use == "trigger phrase"
        assert result.description == "Content.\n\nWhen to use: trigger phrase"

    def test_missing_when_to_use(self) -> None:
        raw = """\
---
name: no-when
description: Just a description.
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.when_to_use == ""
        assert result.description == "Just a description."

    def test_concat_truncated_at_1536(self) -> None:
        """Combined description + when_to_use is capped at the spec's 1536-char budget."""
        long_desc = "A" * 1000
        long_when = "B" * 1000
        raw = f"""\
---
name: long
description: {long_desc}
when_to_use: {long_when}
---

Content.
"""
        result = parse_skill_md(raw)
        assert len(result.description) == 1536
        # The truncation keeps the description prefix; when_to_use is what gets clipped.
        assert result.description.startswith("A" * 1000)


class TestModelAndEffort:
    """SKILL.md spec ``model:`` / ``effort:`` — per-skill overrides."""

    def test_model_extracted(self) -> None:
        raw = """\
---
name: with-model
model: claude-opus-4-7
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.model == "claude-opus-4-7"

    def test_effort_extracted(self) -> None:
        raw = """\
---
name: with-effort
effort: high
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.effort == "high"

    def test_both_default_empty(self) -> None:
        raw = """\
---
name: bare
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.model == ""
        assert result.effort == ""


class TestInvocationControl:
    """SKILL.md spec ``disable-model-invocation:`` + ``user-invocable:``."""

    def test_disable_model_invocation_true(self) -> None:
        raw = """\
---
name: model-blocked
disable-model-invocation: true
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.disable_model_invocation is True
        # ``user_invocable`` defaults to True (spec default).
        assert result.user_invocable is True

    def test_user_invocable_false(self) -> None:
        raw = """\
---
name: hidden
user-invocable: false
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.user_invocable is False
        # ``disable_model_invocation`` defaults to False.
        assert result.disable_model_invocation is False

    def test_both_unset_uses_spec_defaults(self) -> None:
        """Spec default: both invokers can use the skill."""
        raw = """\
---
name: bare
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.disable_model_invocation is False
        assert result.user_invocable is True

    def test_string_true_false_accepted(self) -> None:
        """YAML can quote bools; the parser accepts ``"true"``/``"false"``."""
        raw = """\
---
name: quoted-bools
disable-model-invocation: "true"
user-invocable: "false"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.disable_model_invocation is True
        assert result.user_invocable is False

    def test_yaml_int_accepted(self) -> None:
        """YAML safe_load returns ``int`` for unquoted ``0``/``1``.  Without
        explicit handling these silently fall back to defaults, dropping the
        author's intent."""
        raw = """\
---
name: int-bools
disable-model-invocation: 1
user-invocable: 0
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.disable_model_invocation is True
        assert result.user_invocable is False

    def test_other_ints_fall_back_to_default(self) -> None:
        """Spec recognises only ``0``/``1`` as integer boolean forms.
        ``2`` is ambiguous — silently coercing via Python truthiness
        would disable model invocation on a typo without warning.  Copilot
        review on PR #577 caught the too-permissive original."""
        raw = """\
---
name: ambiguous-int
disable-model-invocation: 2
user-invocable: -1
---

Content.
"""
        result = parse_skill_md(raw)
        # Both fall back to spec defaults (model can autoload, user can pick).
        assert result.disable_model_invocation is False
        assert result.user_invocable is True

    def test_quoted_yaml_1_1_variants(self) -> None:
        """YAML 1.1 spellings — ``yes``/``no``/``on``/``off`` — survive
        quoting.  Unquoted forms get coerced to bool by safe_load (covered
        by ``test_disable_model_invocation_true``), but a quoted variant
        is a plain string that needs the broader match table."""
        raw = """\
---
name: yaml-11-quoted
disable-model-invocation: "yes"
user-invocable: "OFF"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.disable_model_invocation is True
        assert result.user_invocable is False


class TestArgumentsAndHint:
    """SKILL.md spec ``arguments:`` (named positional slots) +
    ``argument-hint:`` (autocomplete display)."""

    def test_yaml_list_format(self) -> None:
        raw = """\
---
name: with-args
arguments: [issue, branch]
---

Fix issue $issue on $branch.
"""
        result = parse_skill_md(raw)
        assert result.arguments == ["issue", "branch"]

    def test_space_delimited_format(self) -> None:
        """Spec accepts space-separated string per the docs sample."""
        raw = """\
---
name: with-args-space
arguments: "issue branch"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.arguments == ["issue", "branch"]

    def test_argument_hint_extracted(self) -> None:
        raw = """\
---
name: with-hint
argument-hint: "[issue-number]"
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.argument_hint == "[issue-number]"

    def test_empty_defaults(self) -> None:
        raw = """\
---
name: bare
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.arguments == []
        assert result.argument_hint == ""


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

    def test_null_description_falls_back_to_body(self) -> None:
        """YAML null description must not produce 'None' string."""
        raw = """\
---
name: null-desc
description:
---

First paragraph here.
"""
        result = parse_skill_md(raw)
        assert result.description == "First paragraph here."
        assert "None" not in result.description

    def test_null_license_and_compatibility(self) -> None:
        """YAML null license/compatibility must not produce 'None' string."""
        raw = """\
---
name: null-fields
description: Test
license:
compatibility:
---

Content.
"""
        result = parse_skill_md(raw)
        assert result.license == ""
        assert result.compatibility == ""


class TestStandardFieldLengths:
    """Spec caps: description <= 1536 (combined w/ when_to_use), compatibility <= 500."""

    def test_description_truncated_at_1536(self) -> None:
        long_desc = "x" * 1700
        raw = f"""\
---
name: long-desc
description: "{long_desc}"
---

Content.
"""
        result = parse_skill_md(raw)
        assert len(result.description) == 1536

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
