"""Operator-context helpers — first-class system turns.

Operator-level context injected mid-session (output-guard findings, user
interjections, metacognitive nudges, skill hints) lives in the conversation
trajectory as first-class ``{"role": "system", "_source": <kind>, "content":
...}`` turns (see :func:`make_system_turn`).  At the wire boundary each turn is
either kept inline (native mid-conversation system messages — claude-opus-4-8)
or folded into the preceding turn as a nonce-delimited ``<system-reminder_
{nonce}>`` fence for every other model.  The fence mechanism (mint / neutralise
/ wrap) lives in :mod:`turnstone.core.fence`, shared with the output-guard judge
so the two trust boundaries cannot drift; ``lowering.fold_system_turns``
applies it.

This module also hosts :func:`parse_priority` (the ``!!!`` priority prefix on
queued user messages) and :func:`render_user_interjection` (the user-authored
framing for a drained queued message).
"""

from __future__ import annotations

from typing import Any, Final

# Priority constants
PRIORITY_IMPORTANT: Final = "important"
PRIORITY_NOTICE: Final = "notice"


def parse_priority(text: str) -> tuple[str, str]:
    """Extract priority prefix from user message text.

    Returns ``(cleaned_text, priority)`` where *priority* is
    ``"important"`` if the message starts with ``!!!`` or ``"notice"``
    otherwise.
    """
    if text.startswith("!!!"):
        return text[3:].lstrip(), PRIORITY_IMPORTANT
    return text, PRIORITY_NOTICE


# User-interjection framing.  A queued user message that drains mid-turn becomes
# a ``user_interjection`` system turn; this preamble keeps it framed as the
# *user's* words, not an operator directive.  That matters most on the native
# mid-conversation-system path, where the turn enters as a real ``role=system``
# message: without the "the user sent…" framing the user's text would inherit
# system/operator authority (an authority inversion).  The priority controls
# urgency wording; the body marker separates the framing from the verbatim body.
_USER_INTERJECTION_NOTICE_PREAMBLE: Final = (
    "The user sent additional context while you were working. "
    "Incorporate if relevant, otherwise continue."
)
_USER_INTERJECTION_IMPORTANT_PREAMBLE: Final = (
    "The user sent a message while you were working. You MUST address this before continuing."
)
_USER_INTERJECTION_BODY_MARKER: Final = "\n\nUser message: "


def render_user_interjection(message: str, priority: str) -> str:
    """Frame a queued user *message* as user-authored operator context.

    Returns ``"{preamble}\\n\\nUser message: {message}"`` — the preamble varies
    by *priority* (``important`` → must-address; otherwise → incorporate-if-
    relevant).  Used by ``ChatSession._collect_advisories`` when turning a
    drained queued message into a ``user_interjection`` system turn.
    """
    preamble = (
        _USER_INTERJECTION_IMPORTANT_PREAMBLE
        if priority == PRIORITY_IMPORTANT
        else _USER_INTERJECTION_NOTICE_PREAMBLE
    )
    return f"{preamble}{_USER_INTERJECTION_BODY_MARKER}{message}"


# -- First-class system turns -------------------------------------------------
# Operator-context (output-guard findings, user interjections, metacognitive
# nudges) lives in the conversation trajectory as real
# ``{"role": "system", "_source": <kind>, "content": ...}`` messages rather
# than spliced into a neighbouring turn's ``content``.  At the wire boundary a
# system turn is either kept inline (native mid-conversation system messages —
# claude-opus-4-8) or folded into the preceding turn as a ``<system-reminder>``
# block (every other model).  ``_source`` classifies the turn for UI rendering
# and replay; it rides the persisted ``_source`` column and is stripped before
# the LLM wire by ``sanitize_messages``.  See ``ChatSession`` for the producers
# and the fold-or-keep pass.

# Canonical ``_source`` values.  ``output_guard`` / ``user_interjection`` /
# ``skill_hint`` come from the advisory producers above (skill_hint via
# ``ChatSession._skill_hint`` queuing onto the tool channel); the rest mirror the
# metacognition nudge types (``turnstone.core.metacognition._NUDGE_MAP``) — kept
# in sync by ``tests/test_tool_advisory.py::TestMakeSystemTurn``.
SYSTEM_TURN_SOURCES: Final = frozenset(
    {
        "output_guard",
        "user_interjection",
        "skill_hint",
        "correction",
        "denial",
        "resume",
        "completion",
        "start",
        "tool_error",
        "repeat",
        "idle_children",
        "watch_triggered",
    }
)


def make_system_turn(source: str, content: str, **meta: Any) -> dict[str, Any]:
    """Build a first-class operator-context system turn for ``self.messages``.

    Returns ``{"role": "system", "_source": source, "content": content}``.
    *source* must be one of :data:`SYSTEM_TURN_SOURCES`.  Extra keyword *meta*
    fields are attached as leading-underscore sibling keys (e.g.
    ``watch_name="ci"`` → ``_watch_name``) so they ride the persisted record
    and the UI projection but are stripped before the LLM wire by
    ``sanitize_messages``.  Keys already underscore-prefixed are kept as-is; a
    meta key that normalises onto a reserved field (``_source``) is rejected so
    the validated source can't be silently clobbered.

    ``content`` is stored and — on the native mid-conversation-system path
    (claude-opus-4-8) — sent to the model verbatim, so fence-escaping is NOT
    done here.  It belongs to the fallback fold step, which wraps the content
    in a nonce-delimited ``<system-reminder_{nonce}>`` fence via
    :func:`turnstone.core.fence.wrap` (applied in
    ``lowering.fold_system_turns``).  Escaping in this builder would corrupt
    the native path, where there is no fence to break out of.
    """
    if source not in SYSTEM_TURN_SOURCES:
        raise ValueError(f"unknown system-turn source: {source!r}")
    turn: dict[str, Any] = {"role": "system", "_source": source, "content": content}
    for key, value in meta.items():
        norm = key if key.startswith("_") else f"_{key}"
        if norm in turn:
            raise ValueError(f"system-turn meta key {key!r} collides with reserved {norm!r}")
        turn[norm] = value
    return turn
