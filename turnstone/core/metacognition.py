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

_NUDGE_MAP: dict[str, str] = {
    "correction": NUDGE_CORRECTION,
    "denial": NUDGE_DENIAL,
    "resume": NUDGE_RESUME,
    "completion": NUDGE_COMPLETION,
    "start": NUDGE_START,
}

# ---------------------------------------------------------------------------
# Detection heuristics
# ---------------------------------------------------------------------------

_CORRECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)^no[,.\s]"),
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

_COMPLETION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)^thanks\b"),
    re.compile(r"(?i)\bthat'?s all\b"),
    re.compile(r"(?i)\blooks good\b"),
    re.compile(r"(?i)^perfect\b"),
    re.compile(r"(?i)^great job\b"),
    re.compile(r"(?i)\bthat works\b"),
    re.compile(r"(?i)^done\b"),
    re.compile(r"(?i)^lgtm\b"),
]


def detect_correction(message: str) -> bool:
    """Return True if the message looks like a user correction."""
    if not message:
        return False
    return any(p.search(message) for p in _CORRECTION_PATTERNS)


def detect_completion(message: str) -> bool:
    """Return True if the message signals session completion."""
    if not message:
        return False
    return any(p.search(message) for p in _COMPLETION_PATTERNS)


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
