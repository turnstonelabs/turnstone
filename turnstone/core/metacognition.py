"""Metacognitive prompting — situational nudges for proactive memory use.

Static nudge text templates (``NUDGE_*``), detection heuristics
(``detect_correction``, ``detect_completion``), the :class:`RepeatDetector`
streak counter, and the cooldown-aware :func:`should_nudge` /
:func:`format_nudge` / :func:`format_idle_children_nudge` helpers.

The wake-trigger lifecycle (``IdleNudgeWatcher`` plus the
``install_idle_nudge_watcher`` / ``shutdown_idle_nudge_watchers``
lifespan helpers) lives in :mod:`turnstone.core.idle_nudge_watcher`.
"""

from __future__ import annotations

import re
import time

# Default cooldown (s) between nudges of the same type.  Production
# paths pass ``cooldown_secs`` explicitly from
# ``MemoryConfig.nudge_cooldown`` (config-store ``memory.nudge_cooldown``,
# default 300); this constant is the fallback for tests and unit-style
# callers without a ``MemoryConfig`` and is kept aligned with that
# canonical default so both paths behave the same.
_COOLDOWN_SECS = 300

# Repeat-detection threshold — number of *consecutive* identical tool
# calls (same name + same arguments) before a repeat warning fires.
# Two-in-a-row is too noisy because legitimate retries on transient
# failures look identical; three-in-a-row is the cheapest signal that
# the model is stuck on the same call.
_REPEAT_THRESHOLD = 3


class RepeatDetector:
    """Detect a streak of identical tool-call signatures.

    ``record(sig)`` returns ``True`` once *sig* has been recorded
    ``threshold`` times in a row (default 3).  Recording a different
    signature resets the streak — interleaved tool calls aren't a
    stuck loop, only repeated identical ones are.  After a fire, the
    caller is expected to call ``clear()`` to start a fresh streak.
    """

    def __init__(self, threshold: int = _REPEAT_THRESHOLD) -> None:
        self._threshold = threshold
        self._sig: str | None = None
        self._count = 0

    def record(self, sig: str) -> bool:
        """Record *sig*; return ``True`` when the streak hits the threshold."""
        if sig == self._sig:
            self._count += 1
        else:
            self._sig = sig
            self._count = 1
        return self._count >= self._threshold

    def clear(self) -> None:
        self._sig = None
        self._count = 0


# ---------------------------------------------------------------------------
# Nudge messages (brief, model-facing hints)
# ---------------------------------------------------------------------------

NUDGE_CORRECTION = (
    "Note: The user's message may contain a correction or preference. "
    "Pay close attention — if they explain what went wrong or how they'd "
    "prefer you to work, consider saving that as a feedback memory "
    "(memory action='save', type='feedback') so you don't repeat this."
)

NUDGE_DENIAL = (
    "Note: The user just rejected a tool action. Their feedback may "
    "explain why — pay attention to whether this reflects a persistent "
    "preference (e.g. 'never use force-push', 'don't modify that file'). "
    "If so, save it as a feedback memory for future sessions."
)

NUDGE_RESUME = (
    "This workstream has prior conversation history. Before proceeding, "
    "use memory(action='search') to check for relevant context — there "
    "may be saved preferences, project notes, or prior decisions that "
    "apply to this work."
)

NUDGE_COMPLETION = (
    "The task may be wrapping up. Consider whether there are learnings, "
    "decisions, or user preferences from this session worth persisting "
    "as memories (memory action='save') so future sessions can benefit."
)

NUDGE_START = (
    "You have saved memories from prior sessions that may be relevant. "
    "Consider using memory(action='search') with keywords from the "
    "user's request to find applicable context, preferences, or guidance."
)

NUDGE_TOOL_ERROR = (
    "A tool just returned an error. Before retrying, check your memories — "
    "the user may have given feedback about this tool or error pattern in a "
    "previous session. Use memory(action='search') to find relevant guidance."
)

NUDGE_REPEAT = (
    "You just called the same tool with the same arguments as a previous "
    "call in this conversation. Repeating the exact same action will produce "
    "the same result. Stop and reconsider your approach — try a different "
    "tool, different arguments, or ask the user for clarification."
)

_NUDGE_MAP: dict[str, str] = {
    "correction": NUDGE_CORRECTION,
    "denial": NUDGE_DENIAL,
    "resume": NUDGE_RESUME,
    "completion": NUDGE_COMPLETION,
    "start": NUDGE_START,
    "tool_error": NUDGE_TOOL_ERROR,
    "repeat": NUDGE_REPEAT,
    # idle_children and watch_triggered carry no static body — the
    # per-fire text comes from a producer (``format_idle_children_nudge``
    # for the former, ``format_watch_message`` + ``sanitize_payload``
    # in the watch dispatch closure for the latter).  Empty string
    # here keeps :func:`format_nudge` round-tripping honestly while
    # still letting :func:`should_nudge` and ``_NUDGE_MAP``-as-registry
    # consumers recognise the type.
    "idle_children": "",
    "watch_triggered": "",
}


# Display cap for the ``idle_children`` body — list at most this many
# children inline, append "...and N more" overflow line beyond that.
NUDGE_IDLE_CHILDREN_DISPLAY_CAP = 6

# Suggested ``wait_for_workstream(ws_ids=[...])`` cap — matches
# ``WAIT_MAX_WS_IDS`` in :mod:`turnstone.core.coordinator_client` so the
# emitted suggestion is callable as-is.
NUDGE_IDLE_CHILDREN_WAIT_CAP = 32

NUDGE_IDLE_CHILDREN_HEADER = (
    "You went idle but still have active child workstreams.  Either "
    "continue the user's work or block on the listed children "
    "explicitly:"
)


# ASCII control chars + Unicode steering vectors (bidi-override,
# zero-width, line/paragraph separators, BOM, tag chars).  Treated
# uniformly as control chars and replaced with a space; angle-bracket
# tag-breakers are stripped separately below.  Defense-in-depth today
# (self-injection within one user's tenant — children inherit parent
# ``user_id`` and watch commands are user-supplied), but becomes
# load-bearing the moment a producer ingests payloads from a different
# trust boundary (a future watch trigger consuming external webhook
# bodies, etc).
#
# Two classes, picked at the call site by the caller's structural
# requirements:
#   * :data:`_NAME_CONTROL_CHARS` — STRICT: also strips TAB/LF/CR.
#     Used by :func:`sanitize_name` for single-line user-controlled
#     fields (workstream ``name`` rendered as bullet items by
#     :func:`format_idle_children_nudge` — a name with ``\n`` in it
#     would otherwise break the bullet's one-line structure and let
#     a malicious child name forge sibling rows).
#   * :data:`_PAYLOAD_CONTROL_CHARS` — PERMISSIVE: preserves TAB/LF/CR.
#     Used by :func:`sanitize_payload` for multi-line payloads where
#     line layout is part of the signal (watch shell output —
#     stripping LF/CR would collapse multi-line output to one line).
_CONTROL_CHARS_TAIL = (
    r"\u200b-\u200f"  # zero-width / LRM / RLM
    r"\u202a-\u202e"  # bidi overrides
    r"\u2066-\u2069"  # bidi isolates
    r"\u2028\u2029"  # line / paragraph separator
    r"\ufeff"  # BOM
    r"]"
    r"|[\U000e0000-\U000e007f]"  # Unicode tag chars (separate range above BMP)
)
_NAME_CONTROL_CHARS = re.compile(
    r"[\x00-\x1f\x7f" + _CONTROL_CHARS_TAIL  # ASCII control (incl. \t\n\r) + DEL
)
_PAYLOAD_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f" + _CONTROL_CHARS_TAIL  # ASCII control (skip \t\n\r) + DEL
)
_PAYLOAD_TAG_BREAKERS = re.compile(r"[<>]")


def sanitize_name(text: str) -> str:
    """Strict sanitiser for single-line user-controlled name fields.

    Strips ASCII control chars **including** TAB/LF/CR plus Unicode
    steering vectors and angle-bracket tag breakers.  Use for fields
    rendered as a single bullet item / label where embedded newlines
    would break the surrounding structure (workstream ``name`` in
    :func:`format_idle_children_nudge`, where a ``\\n`` in the name
    would otherwise forge a fake sibling bullet).
    """
    if not text:
        return ""
    cleaned = _NAME_CONTROL_CHARS.sub(" ", text)
    cleaned = _PAYLOAD_TAG_BREAKERS.sub("", cleaned)
    return cleaned.strip()


def sanitize_payload(text: str) -> str:
    """Permissive sanitiser for multi-line user-controlled nudge payloads.

    Used by ``format_watch_message`` output rendered into the
    ``watch_triggered`` nudge body.

    The wire-boundary :func:`escape_wrapper_tags` only protects the
    ``<system-reminder>`` and ``<tool_output>`` envelopes; other
    angle-bracketed markers (``</thinking>``, ``<answer>``,
    ``<artifact>``, …) and Unicode steering vectors (RTL override,
    zero-width chars, tag chars) can still steer some models.  Strip
    both classes before interpolation — self-injection only today
    (watch commands are user-supplied), but the cost is one ``re.sub``
    per payload.

    TAB / LF / CR are preserved (see ``_PAYLOAD_CONTROL_CHARS``) so
    multi-line shell output in watch payloads keeps its line structure.
    For single-line name fields where newlines would break surrounding
    structure, use :func:`sanitize_name` instead.
    """
    if not text:
        return ""
    cleaned = _PAYLOAD_CONTROL_CHARS.sub(" ", text)
    cleaned = _PAYLOAD_TAG_BREAKERS.sub("", cleaned)
    return cleaned.strip()


def format_idle_children_nudge(children: list[dict[str, str]]) -> str:
    """Render the ``idle_children`` reminder body.

    *children* is a list of dicts with ``ws_id``, ``name``, ``state``
    keys — the row-mapping shape coordinator-side storage exposes.
    Returns raw text *without* the ``<system-reminder>`` envelope; the
    side-channel :func:`_apply_reminders_for_provider` splice wraps it
    at the wire boundary.

    User-controlled ``name`` strings get sanitized via
    :func:`sanitize_name` before interpolation so a workstream
    named ``</thinking>...`` can't steer the model's reasoning
    channels through the rendered body, and an embedded ``\\n`` in
    a name can't forge a fake sibling bullet.

    Display caps at :data:`NUDGE_IDLE_CHILDREN_DISPLAY_CAP` with an
    overflow line; the trailing ``wait_for_workstream`` suggestion's
    ``ws_ids`` list caps at :data:`NUDGE_IDLE_CHILDREN_WAIT_CAP`.
    Empty input returns the empty string so callers can short-circuit
    on ``if not text: return``.
    """
    if not children:
        return ""
    lines = [NUDGE_IDLE_CHILDREN_HEADER, ""]
    shown = children[:NUDGE_IDLE_CHILDREN_DISPLAY_CAP]
    for c in shown:
        ws_id = c.get("ws_id", "")
        name = sanitize_name(c.get("name", "")) or "(unnamed)"
        state = c.get("state", "?")
        lines.append(f"  - {ws_id[:8]} ({state}): {name}")
    overflow = len(children) - len(shown)
    if overflow > 0:
        lines.append(f"  ...and {overflow} more")
    lines.append("")
    wait_ids = [c.get("ws_id", "") for c in children[:NUDGE_IDLE_CHILDREN_WAIT_CAP]]
    lines.append(
        f'To block on them: wait_for_workstream(ws_ids={wait_ids!r}, mode="any", timeout=120).'
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Detection heuristics — strong/weak tiers
#
# Strong patterns fire unconditionally.  Weak patterns carry inherent
# ambiguity ("no …", "thanks …") and only fire when the surrounding
# message looks like a genuine correction/completion rather than normal
# conversation.
# ---------------------------------------------------------------------------

_STRONG_CORRECTION: list[re.Pattern[str]] = [
    re.compile(r"(?i)^no[,.]"),  # "no," / "no." — clear rejection
    re.compile(r"(?i)\bdon'?t\b"),
    re.compile(r"(?i)^stop\b"),
    re.compile(r"(?i)^actually[,\s]"),
    re.compile(r"(?i)^instead[,\s]"),
    re.compile(r"(?i)\bnot like that\b"),
    re.compile(r"(?i)^wrong\b"),
    re.compile(r"(?i)\bthat'?s not\b"),
    re.compile(r"(?i)^I said\b"),
    re.compile(r"(?i)^I meant\b"),
    re.compile(r"(?i)\bnever\b.*\balways\b"),
    re.compile(r"(?i)^please don'?t\b"),
]

# "no <word>" is ambiguous — only match when the next word is a pronoun,
# demonstrative, article, or verb that signals the user is redirecting,
# not a fixed phrase like "no problem" or "no worries".  Allowlist >
# blocklist: we don't need to enumerate every benign "no X" phrase.
_WEAK_CORRECTION: list[re.Pattern[str]] = [
    re.compile(
        r"(?i)^no\s+(?:I\b|you\b|we\b|they\b|it\b|he\b|she\b"
        r"|that\b|this\b|those\b|these\b"
        r"|the\b|a\b|an\b"
        r"|not\b|do\b|did\b|but\b)"
    ),
]

_STRONG_COMPLETION: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bthat'?s all\b"),
    re.compile(r"(?i)^lgtm\b"),
]

# These patterns are common in both completion AND mid-conversation
# acknowledgment.  Only fire when the message is short and has no
# continuation markers (question marks, follow-up requests).
_WEAK_COMPLETION: list[re.Pattern[str]] = [
    re.compile(r"(?i)^thanks\b(?!\s+for\b)"),  # "thanks for X" = acknowledgment
    re.compile(r"(?i)\blooks good\b"),
    re.compile(r"(?i)^perfect\b"),
    re.compile(r"(?i)^great job\b"),
    re.compile(r"(?i)\bthat works\b"),
    re.compile(r"(?i)^done\b"),
]

_WEAK_MSG_CAP = 80  # weak completion patterns suppressed above this length

_CONTINUATION = re.compile(
    r"(?i)(?:\?|(?:can you|could you|please\s|also\s|but\s|now\s|next\s"
    r"|and\s+then|after\s+that|one\s+more|however))"
)


def detect_correction(message: str) -> bool:
    """Return True if the message looks like a user correction."""
    if not message:
        return False
    if any(p.search(message) for p in _STRONG_CORRECTION):
        return True
    return any(p.search(message) for p in _WEAK_CORRECTION)


def detect_completion(message: str) -> bool:
    """Return True if the message signals session completion."""
    if not message:
        return False
    if any(p.search(message) for p in _STRONG_COMPLETION):
        return True
    if len(message) > _WEAK_MSG_CAP:
        return False
    if _CONTINUATION.search(message):
        return False
    return any(p.search(message) for p in _WEAK_COMPLETION)


def _cooldown_allows(
    nudge_type: str,
    state: dict[str, float],
    *,
    cooldown_secs: int = _COOLDOWN_SECS,
) -> bool:
    """Read-only cooldown peek — does NOT record a fire timestamp.

    Use this as a cheap pre-gate before expensive work (storage queries,
    message walks).  The follow-up :func:`should_nudge` call re-checks
    cooldown AND records the timestamp atomically.  A producer that
    races between this peek and ``should_nudge`` would just lose the
    fire to the other producer — benign.
    """
    last = state.get(nudge_type)
    if last is None:
        return True
    return time.monotonic() - last >= cooldown_secs


def should_nudge(
    nudge_type: str,
    state: dict[str, float],
    *,
    message_count: int = 0,
    memory_count: int = 0,
    cooldown_secs: int = _COOLDOWN_SECS,
) -> bool:
    """Check whether a nudge should fire, respecting cooldowns and context."""
    if nudge_type not in _NUDGE_MAP:
        return False
    # Don't nudge on the very first message (except resume/start)
    if message_count <= 1 and nudge_type not in ("resume", "start"):
        return False
    # Start nudge only on first message
    if nudge_type == "start" and message_count != 1:
        return False
    # Tool error nudge only if there are memories to search
    if nudge_type == "tool_error" and memory_count == 0:
        return False
    # Resume/start nudge only if there are memories to recall
    if nudge_type in ("resume", "start") and memory_count == 0:
        return False
    # Rate limit: one nudge per type per cooldown window
    now = time.monotonic()
    last = state.get(nudge_type)
    if last is not None and now - last < cooldown_secs:
        return False
    state[nudge_type] = now
    return True


def format_nudge(nudge_type: str) -> str:
    """Return the nudge text for the given type."""
    return _NUDGE_MAP.get(nudge_type, "")
