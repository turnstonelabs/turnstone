"""SKILL.md parser — extract structured metadata from skill definition files.

Pure functions, no I/O.  Accepts raw SKILL.md text and returns a
:class:`ParsedSkill` dataclass.

Compliant with the Agent Skills specification (https://agentskills.io/specification).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, overload

import frontmatter

from turnstone.core.log import get_logger

log = get_logger(__name__)

# Name validation: lowercase letters, digits, hyphens, max 64 chars.
# Note: consecutive hyphens checked separately (not expressible in a
# single character-class regex without a lookahead).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$")

# Split allowed-tools on whitespace or commas (standard uses spaces,
# legacy turnstone format uses commas).  Tool expressions must not
# contain internal whitespace (e.g. "Bash(git:*)" not "Bash(git: *)").
_LIST_SPLIT_RE = re.compile(r"[\s,]+")

# Malformed YAML recovery: match a bare ``description:`` line whose
# value contains an unquoted colon (the most common cross-client issue).
_BARE_DESC_RE = re.compile(r"^(description:\s*)(.+)$", re.MULTILINE)

# Field length caps.  ``MAX_SKILL_DESCRIPTION_LEN`` matches the
# SKILL.md spec's combined ``description`` +
# ``when_to_use`` listing budget (1,536 chars).  Exported (no leading
# underscore) because the same cap must be enforced at every write
# surface — Pydantic schemas, the admin HTTP handlers, and the
# coordinator ``skills`` tool — and a magic number repeated in five
# places is a desync waiting to happen.
MAX_SKILL_DESCRIPTION_LEN = 1536
_MAX_COMPATIBILITY_LEN = 500


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
    # SKILL.md spec ``paths:`` — glob patterns gating
    # model-initiated autoload.  Filter consumer lands in a follow-up
    # PR (issue #569); parsed here so the value round-trips through
    # install / admin edit / export without loss.
    paths: list[str] = field(default_factory=list)
    # SKILL.md spec ``when_to_use:`` — additional trigger context for
    # when the skill should be invoked.  Concatenated into
    # ``description`` (capped at ``MAX_SKILL_DESCRIPTION_LEN``) so the model
    # sees both in the listing; kept here separately for the admin
    # parse-preview UI which surfaces it as its own field.
    when_to_use: str = ""
    # SKILL.md spec ``model:`` and ``effort:`` — per-skill model
    # override + reasoning effort.  Fields keep their spec names here
    # for fidelity at the parser layer; the install handler translates
    # ``effort`` → ``prompt_templates.reasoning_effort`` at the storage
    # boundary.  Seeding fires only on initial create — re-install
    # short-circuits at the source_url dedup so admin overrides survive.
    model: str = ""
    effort: str = ""
    # SKILL.md spec ``disable-model-invocation:`` and ``user-invocable:``
    # — invocation-control axes.  Stored in raw spec shape on the
    # dataclass; defaults match spec (both invokers can use the skill
    # unless gated).
    #
    # ``user_invocable=False`` is the load-bearing one: the install
    # handler derives ``hidden_from_menu=True`` from it, and
    # ``list_skills_summary`` filters those rows out of the
    # user-facing picker.  Round-trip works end to end.
    #
    # ``disable_model_invocation=True`` has NO install consumer today —
    # Turnstone's install path hardcodes ``activation="named"`` already,
    # so the spec field's intended translation is a no-op at create
    # time.  We still parse it for fidelity (surface on the
    # parse-preview UI, preserve in ``raw_frontmatter``) so an admin
    # reviewing a SKILL.md sees what the author wrote.  If install
    # ever supports a non-"named" default activation, this is the
    # field that gates flipping back.
    disable_model_invocation: bool = False
    user_invocable: bool = True
    # SKILL.md spec ``arguments:`` — named positional argument slots
    # that pair with ``$<name>`` substitution in the skill body.
    # Accepts the spec's space-separated string or YAML list shape.
    # Stored as a JSON-array column on ``prompt_templates`` (added by
    # migration 056); the renderer in ``session._substitute_skill_args``
    # binds positional args to these names at skill-load time.
    arguments: list[str] = field(default_factory=list)
    # SKILL.md spec ``argument-hint:`` — display string for slash-
    # command autocomplete, e.g. ``"[issue-number]"``.  Surfaced in
    # the admin UI and round-tripped through install; no runtime
    # behaviour today since Turnstone doesn't have a slash-command
    # autocomplete surface yet.
    argument_hint: str = ""
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)


def _extract_tags(meta: dict[str, Any]) -> list[str]:
    """Extract tags from frontmatter, handling both nested-metadata and Hermes formats."""
    # Direct tags field
    tags = meta.get("tags")
    if isinstance(tags, list):
        return [str(t) for t in tags if t]

    # Nested metadata.tags (SKILL.md spec format)
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


def _extract_str(meta: dict[str, Any], key: str, default: str = "") -> str:
    """Extract a string field, checking top-level then ``metadata.*`` fallback.

    Handles YAML ``null`` / bare keys gracefully (returns *default*
    rather than the string ``"None"``).
    """
    raw = meta.get(key)
    val = str(raw).strip() if raw is not None else ""
    if val:
        return val
    # Standard puts author/version under metadata map
    nested = meta.get("metadata")
    if isinstance(nested, dict):
        raw = nested.get(key)
        val = str(raw).strip() if raw is not None else ""
        if val:
            return val
    return default


_YAML_BOOL_TRUE = frozenset({"true", "yes", "on", "1"})
_YAML_BOOL_FALSE = frozenset({"false", "no", "off", "0"})


def _extract_bool(meta: dict[str, Any], key: str, *, default: bool) -> bool:
    """Extract a boolean field from frontmatter.

    Accepts every shape a YAML 1.1 author or YAML library can plausibly
    produce for a boolean value:

    * Python ``bool`` — the natural unquoted ``true``/``false``/``yes``/
      ``no``/``on``/``off`` (case-insensitive) coerced by ``safe_load``.
    * Python ``int`` — unquoted ``1`` or ``0`` (``safe_load`` returns
      ``int`` for these, not ``bool``).
    * Python ``str`` — quoted variants (e.g. ``"true"``, ``"YES"``,
      ``"off"``, ``"0"``) where the author wrapped the value to dodge
      YAML interpretation.

    Anything else (lists, dicts, unknown strings, ``None``) falls
    back to *default*.  The asymmetry between quoted and unquoted
    would silently drop the author's intent if we only matched the
    canonical ``"true"`` / ``"false"`` pair (case study: ``/review``
    on PR #571 caught this gap).
    """
    raw = meta.get(key)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        # ``isinstance(True, int)`` is also True, but the bool branch
        # above already handled that — anything reaching here is an
        # actual int.  Spec mentions only ``0`` / ``1`` as the integer
        # boolean forms; other ints (``2``, ``-1``, ...) are ambiguous
        # and fall back to *default* rather than silently coercing via
        # Python truthiness.  ``/review`` on PR #577 caught the
        # too-permissive coerce — a SKILL.md with ``disable-model-
        # invocation: 2`` would otherwise silently disable model
        # invocation without warning the author about the typo.
        if raw == 0:
            return False
        if raw == 1:
            return True
        return default
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in _YAML_BOOL_TRUE:
            return True
        if lowered in _YAML_BOOL_FALSE:
            return False
    return default


def _extract_list(meta: dict[str, Any], *keys: str) -> list[str]:
    """Extract a list of strings from frontmatter.

    Tries each *key* in order (first match wins).  String values are
    split on whitespace or commas to handle both the Agent Skills
    standard (space-delimited) and legacy comma-delimited formats.
    """
    for key in keys:
        val = meta.get(key)
        if isinstance(val, list):
            return [str(v) for v in val if v]
        if isinstance(val, str) and val:
            return [v for v in _LIST_SPLIT_RE.split(val) if v]
    return []


def validate_skill_name(name: str) -> str | None:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "name is required"
    if len(name) > 64:
        return f"name exceeds 64 characters ({len(name)})"
    if "--" in name:
        return "name must not contain consecutive hyphens"
    if not _NAME_RE.match(name):
        return "name must be lowercase alphanumeric with hyphens (e.g. 'code-review')"
    return None


def _try_parse_frontmatter(raw: str) -> frontmatter.Post:
    """Parse YAML frontmatter with a single malformed-YAML retry.

    The most common cross-client issue is unquoted description values
    containing colons (e.g. ``description: Use when: the user asks``).
    On initial failure, wrap the description value in quotes and retry.
    """
    try:
        return frontmatter.loads(raw)
    except Exception:
        pass  # fall through to retry

    # Retry: quote the description line
    def _quote_desc(m: re.Match[str]) -> str:
        prefix = m.group(1)
        value = m.group(2).strip()
        escaped = value.replace('"', '\\"')
        return f'{prefix}"{escaped}"'

    fixed = _BARE_DESC_RE.sub(_quote_desc, raw)
    if fixed != raw:
        try:
            return frontmatter.loads(fixed)
        except Exception:
            pass

    raise ValueError("Failed to parse SKILL.md YAML frontmatter")


@overload
def parse_skill_md(raw: str, *, lenient: Literal[False] = ...) -> ParsedSkill: ...


@overload
def parse_skill_md(raw: str, *, lenient: Literal[True]) -> ParsedSkill | None: ...


def parse_skill_md(raw: str, *, lenient: bool = False) -> ParsedSkill | None:
    """Parse SKILL.md (YAML frontmatter + markdown body).

    When *lenient* is ``False`` (default — strict mode), raises
    ``ValueError`` on missing/invalid name or unparseable YAML.

    When *lenient* is ``True`` (for external import / cross-client
    ingestion), logs warnings and returns ``None`` for unskippable
    failures instead of raising.
    """
    try:
        post = _try_parse_frontmatter(raw)
    except Exception as exc:
        if lenient:
            log.warning("skill_parser.yaml_failed", error=str(exc))
            return None
        raise ValueError(f"Failed to parse SKILL.md frontmatter: {exc}") from exc

    meta: dict[str, Any] = dict(post.metadata)
    body = post.content.strip()

    # Required: name
    name = str(meta.get("name", "")).strip().lower()
    name_err = validate_skill_name(name)
    if name_err:
        if lenient:
            log.warning("skill_parser.name_invalid", name=name, error=name_err)
            # Try to salvage: strip invalid chars, truncate
            sanitized = re.sub(r"[^a-z0-9-]", "", name).strip("-")
            sanitized = re.sub(r"-{2,}", "-", sanitized)[:64].strip("-")
            if not sanitized or validate_skill_name(sanitized):
                return None
            name = sanitized
        else:
            raise ValueError(name_err)

    # Description — frontmatter or first paragraph of body
    raw_desc = meta.get("description")
    description = str(raw_desc).strip() if raw_desc is not None else ""
    if not description and body:
        first_line = body.split("\n", 1)[0].strip()
        # Skip markdown headings
        if first_line.startswith("#"):
            first_line = first_line.lstrip("# ").strip()
        description = first_line[:256]

    # SKILL.md spec ``when_to_use:`` — appended to description so the
    # model sees both signals on the listing.  Separated by a blank
    # line + "When to use:" prefix; budgeted against the combined cap
    # below so the truncation never lands inside the separator and
    # leaves a dangling "When " or similar partial label.
    when_to_use = _extract_str(meta, "when_to_use")
    if when_to_use:
        if description:
            separator = "\n\nWhen to use: "
            # Reserve room for at least one character of ``when_to_use``
            # past the separator; below that, dropping the addition is
            # cleaner than emitting a trailing-separator description.
            available = MAX_SKILL_DESCRIPTION_LEN - len(description) - len(separator)
            if available > 0:
                description = f"{description}{separator}{when_to_use[:available]}"
        else:
            description = f"When to use: {when_to_use}"

    if not description and lenient:
        log.warning("skill_parser.no_description", name=name)
        return None

    # Spec caps (description + when_to_use combined)
    if len(description) > MAX_SKILL_DESCRIPTION_LEN:
        log.warning(
            "skill_parser.description_truncated",
            name=name,
            length=len(description),
        )
        description = description[:MAX_SKILL_DESCRIPTION_LEN]

    raw_compat = meta.get("compatibility")
    compatibility = str(raw_compat).strip() if raw_compat is not None else ""
    if len(compatibility) > _MAX_COMPATIBILITY_LEN:
        log.warning(
            "skill_parser.compatibility_truncated",
            name=name,
            length=len(compatibility),
        )
        compatibility = compatibility[:_MAX_COMPATIBILITY_LEN]

    return ParsedSkill(
        name=name,
        description=description,
        content=body,
        tags=_extract_tags(meta),
        author=_extract_str(meta, "author"),
        version=_extract_str(meta, "version", default="1.0.0"),
        # Standard uses "allowed-tools" (hyphenated); stored internally as allowed_tools
        allowed_tools=_extract_list(meta, "allowed-tools"),
        license=_extract_str(meta, "license"),
        compatibility=compatibility,
        # Spec accepts ``paths:`` as a comma-separated string or YAML
        # list; ``_extract_list`` handles both shapes via ``_LIST_SPLIT_RE``.
        paths=_extract_list(meta, "paths"),
        when_to_use=when_to_use,
        model=_extract_str(meta, "model"),
        effort=_extract_str(meta, "effort"),
        # Invocation-control axes — spec defaults: model can
        # autoload (disable-model-invocation=False) AND user can pick
        # from the menu (user-invocable=True).
        disable_model_invocation=_extract_bool(meta, "disable-model-invocation", default=False),
        user_invocable=_extract_bool(meta, "user-invocable", default=True),
        # Spec accepts ``arguments:`` as a space-separated string or
        # YAML list; ``_extract_list`` handles both via ``_LIST_SPLIT_RE``.
        arguments=_extract_list(meta, "arguments"),
        # ``argument-hint`` (hyphenated per spec) — display string for
        # slash-command autocomplete.
        argument_hint=_extract_str(meta, "argument-hint"),
        raw_frontmatter=meta,
    )
