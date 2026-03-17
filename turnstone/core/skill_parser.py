"""SKILL.md parser — extract structured metadata from skill definition files.

Pure functions, no I/O.  Accepts raw SKILL.md text and returns a
:class:`ParsedSkill` dataclass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import frontmatter

# Name validation: lowercase letters, digits, hyphens, max 64 chars
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$")


@dataclass(frozen=True)
class ParsedSkill:
    """Structured representation of a SKILL.md file."""

    name: str
    description: str
    content: str  # markdown body (after frontmatter)
    tags: list[str] = field(default_factory=list)
    author: str = ""
    version: str = "1.0.0"
    allowed_tools: list[str] = field(default_factory=list)
    license: str = ""
    compatibility: str = ""
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)


def _extract_tags(meta: dict[str, Any]) -> list[str]:
    """Extract tags from frontmatter, handling both Anthropic and Hermes formats."""
    # Direct tags field
    tags = meta.get("tags")
    if isinstance(tags, list):
        return [str(t) for t in tags if t]

    # Nested metadata.tags (Anthropic format)
    metadata = meta.get("metadata")
    if isinstance(metadata, dict):
        nested = metadata.get("tags")
        if isinstance(nested, list):
            return [str(t) for t in nested if t]
        # metadata.hermes.tags (Hermes format)
        hermes = metadata.get("hermes")
        if isinstance(hermes, dict):
            hermes_tags = hermes.get("tags")
            if isinstance(hermes_tags, list):
                return [str(t) for t in hermes_tags if t]

    return []


def _extract_list(meta: dict[str, Any], key: str) -> list[str]:
    """Extract a list of strings from frontmatter, with fallback."""
    val = meta.get(key)
    if isinstance(val, list):
        return [str(v) for v in val if v]
    if isinstance(val, str) and val:
        return [v.strip() for v in val.split(",") if v.strip()]
    return []


def validate_skill_name(name: str) -> str | None:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "name is required"
    if len(name) > 64:
        return f"name exceeds 64 characters ({len(name)})"
    if not _NAME_RE.match(name):
        return "name must be lowercase alphanumeric with hyphens (e.g. 'code-review')"
    return None


def parse_skill_md(raw: str) -> ParsedSkill:
    """Parse SKILL.md (YAML frontmatter + markdown body).

    Handles missing or malformed frontmatter gracefully — returns a
    ``ParsedSkill`` with defaults for any missing fields.

    Raises ``ValueError`` if ``name`` is missing or invalid.
    """
    try:
        post = frontmatter.loads(raw)
    except Exception as exc:
        raise ValueError(f"Failed to parse SKILL.md frontmatter: {exc}") from exc

    meta: dict[str, Any] = dict(post.metadata)
    body = post.content.strip()

    # Required: name
    name = str(meta.get("name", "")).strip().lower()
    name_err = validate_skill_name(name)
    if name_err:
        raise ValueError(name_err)

    # Description — frontmatter or first paragraph of body
    description = str(meta.get("description", "")).strip()
    if not description and body:
        first_line = body.split("\n")[0].strip()
        # Skip markdown headings
        if first_line.startswith("#"):
            first_line = first_line.lstrip("# ").strip()
        description = first_line[:256]

    return ParsedSkill(
        name=name,
        description=description,
        content=body,
        tags=_extract_tags(meta),
        author=str(meta.get("author", "")).strip(),
        version=str(meta.get("version", "1.0.0")).strip(),
        allowed_tools=_extract_list(meta, "allowed_tools"),
        license=str(meta.get("license", "")).strip(),
        compatibility=str(meta.get("compatibility", "")).strip(),
        raw_frontmatter=meta,
    )
