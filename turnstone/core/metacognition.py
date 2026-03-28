"""Metacognitive prompting — situational nudges for proactive memory use."""

from __future__ import annotations

import re
import time

_COOLDOWN_SECS = 300  # 5 minutes between nudges of the same type

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
}

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
