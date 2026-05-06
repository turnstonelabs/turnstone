"""Metacognitive prompting — situational nudges for proactive memory use.

Also hosts :class:`IdleNudgeWatcher`, the wake-trigger that converts a
workstream's transition to ``WorkstreamState.IDLE`` into a synthetic
empty-user-turn ``send`` whenever the session has any-channel nudges
queued (typically the ``idle_children`` nudge enqueued by the
coordinator-side observer).  Race analysis lives on
:class:`IdleNudgeWatcher` itself.
"""

from __future__ import annotations

import contextlib
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session_manager import SessionManager
    from turnstone.core.workstream import WorkstreamState

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


class IdleNudgeWatcher:
    """Convert a workstream IDLE transition into a wake send when the
    session has queued nudges.

    Subscribes to :meth:`SessionManager.subscribe_to_state` and listens
    for ``WorkstreamState.IDLE``.  If the workstream's
    :class:`NudgeQueue` is non-empty, dispatches via
    ``session_worker.send`` with a no-op ``enqueue`` callback.

    **Race semantics.**  ``session_worker.send`` decides atomically
    under ``ws._lock`` whether a worker thread already owns the
    workstream.  Three outcomes:

    * No worker → spawn a new daemon that calls
      :meth:`ChatSession.deliver_wake_nudge_from_queue` (the wake
      drains its own queue and runs the synthetic empty-user turn).
    * Worker running → call our ``enqueue`` lambda, which is a no-op.
      The wake is silently dropped; the queued nudge stays in
      ``NudgeQueue`` and the in-flight worker picks it up at its next
      user-message-attach or tool-result seam (whichever fires first
      for the entry's channel).  This is the load-bearing fallback —
      we never spawn a competing worker.
    * Workstream gone (``ws is None``) or session not built
      (``ws.session is None``) → bail.

    **Subscription order matters.**  When a workstream-kind-specific
    observer (e.g. ``CoordinatorIdleObserver`` in PR 3) needs to
    *enqueue* a nudge on the same IDLE event before this watcher
    *peeks* the queue, the observer must register first so that
    ``SessionManager.set_state``'s subscriber loop fires it earlier in
    the same synchronous fan-out.

    **Kind-agnostic.**  Fires for any workstream regardless of
    :class:`WorkstreamKind`.  Producers decide what to enqueue.
    """

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self._callback: Callable[[str, WorkstreamState], None] | None = None

    def start(self) -> None:
        """Idempotent — registering twice is a no-op."""
        if self._callback is not None:
            return
        # Local import to avoid a circular import at module load time
        # (session_manager imports from session, which already pulls
        # metacognition, so a top-level import here would cycle).
        from turnstone.core.workstream import WorkstreamState

        def _on_state(ws_id: str, state: WorkstreamState) -> None:
            if state is not WorkstreamState.IDLE:
                return
            ws = self._manager.get(ws_id)
            if ws is None or ws.session is None:
                return
            session = ws.session
            if len(session._nudge_queue) == 0:
                return
            from turnstone.core import session_worker

            session_worker.send(
                ws,
                enqueue=lambda: None,
                run=session.deliver_wake_nudge_from_queue,
                thread_name=f"wake-nudge-{ws.id[:8]}",
            )

        self._callback = _on_state
        self._manager.subscribe_to_state(_on_state)

    def shutdown(self) -> None:
        """Unsubscribe; idempotent."""
        cb = self._callback
        if cb is None:
            return
        with contextlib.suppress(Exception):
            self._manager.unsubscribe_from_state(cb)
        self._callback = None


_APP_STATE_ATTR = "_idle_nudge_watchers"


def install_idle_nudge_watcher(app: Any, manager: SessionManager) -> IdleNudgeWatcher:
    """Construct + start an :class:`IdleNudgeWatcher` and register it
    for lifespan teardown via :func:`shutdown_idle_nudge_watchers`.

    Multiple watchers may be installed against different
    :class:`SessionManager` instances on the same ``app`` (e.g. the
    interactive manager + the coord manager on a multi-kind host).
    All of them get torn down by a single
    :func:`shutdown_idle_nudge_watchers` call.

    Returns the watcher so the caller can run additional setup
    against the same manager — but the typical site doesn't need
    the return value.
    """
    watcher = IdleNudgeWatcher(manager)
    watcher.start()
    watchers: list[IdleNudgeWatcher] = getattr(app.state, _APP_STATE_ATTR, [])
    if not watchers:
        # First watcher on this app — initialise the list.  Avoids
        # mutating a default arg or sharing the list across apps.
        setattr(app.state, _APP_STATE_ATTR, watchers)
    watchers.append(watcher)
    return watcher


def shutdown_idle_nudge_watchers(app: Any) -> None:
    """Shut down every watcher installed via
    :func:`install_idle_nudge_watcher`.  No-op if none.
    """
    watchers: list[IdleNudgeWatcher] = getattr(app.state, _APP_STATE_ATTR, [])
    for watcher in watchers:
        watcher.shutdown()
    watchers.clear()
