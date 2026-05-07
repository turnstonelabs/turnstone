"""Tool result advisory system — inject contextual advisories into tool output.

When advisories are present (output guard findings, queued user messages, etc.),
the raw tool output is wrapped in ``<tool_output>`` tags and each advisory is
appended as a ``<system-reminder>`` block.  When there are no advisories, the
raw output passes through unchanged (zero overhead).

The wrapper pattern is intentionally general: any feature that needs to
communicate out-of-band context to the model at the tool-result boundary can
produce a ``ToolAdvisory`` and feed it through ``wrap_tool_result()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

if TYPE_CHECKING:
    from turnstone.core.output_guard import OutputAssessment

# Priority constants
PRIORITY_IMPORTANT: Final = "important"
PRIORITY_NOTICE: Final = "notice"


# UserInterjection preamble strings — shared between the producer
# (``UserInterjection.render``) and the replay parser
# (``history_decoration._classify_advisory``).  Keeping them in one
# place ensures the parser and producer can never drift on the exact
# wording the parser uses to disambiguate priority.  Marker is the
# fixed substring that separates the preamble from the user's body.
_USER_INTERJECTION_NOTICE_PREAMBLE: Final = (
    "The user sent additional context while you were working. "
    "Incorporate if relevant, otherwise continue."
)
_USER_INTERJECTION_IMPORTANT_PREAMBLE: Final = (
    "The user sent a message while you were working. You MUST address this before continuing."
)
_USER_INTERJECTION_BODY_MARKER: Final = "\n\nUser message: "


# -- Protocol -----------------------------------------------------------------


@runtime_checkable
class ToolAdvisory(Protocol):
    """Anything that can render advisory text for injection into a tool result."""

    @property
    def advisory_type(self) -> str: ...

    def render(self) -> str: ...


# -- Concrete advisory types --------------------------------------------------


@dataclass(frozen=True)
class GuardAdvisory:
    """Advisory produced by the output guard when a tool result is flagged."""

    assessment: OutputAssessment
    func_name: str

    @property
    def advisory_type(self) -> str:
        return "output_guard"

    def render(self) -> str:
        a = self.assessment
        lines = [
            f"Output guard: {', '.join(a.flags)} ({a.risk_level.upper()})",
        ]
        for ann in a.annotations:
            lines.append(f"  {ann}")
        if a.sanitized is not None:
            lines.append(
                "Credentials have been redacted. Do not attempt to reconstruct redacted values."
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class UserInterjection:
    """Advisory for a message the user sent while the model was executing.

    Produced by ``ChatSession._collect_advisories`` on the LAST tool
    result of a batch when ``_queued_messages`` is non-empty (Seam 1 in
    the queued-message architecture).  Splicing inside the tool-result
    envelope keeps the role sequence ``assistant -> tool`` intact for
    strict-template providers and delivers the user's text on the
    same turn as the tool batch.  The persisted DB row is the wrapped
    envelope — replay extracts the advisory back out via
    :func:`history_decoration.decorate_history_messages` and renders
    it as a proper user bubble in the wire/UI layer, so the queued
    message survives reconnect / page reload.
    """

    message: str
    priority: str = PRIORITY_NOTICE

    @property
    def advisory_type(self) -> str:
        return "user_interjection"

    def render(self) -> str:
        if self.priority == PRIORITY_IMPORTANT:
            preamble = _USER_INTERJECTION_IMPORTANT_PREAMBLE
        else:
            preamble = _USER_INTERJECTION_NOTICE_PREAMBLE
        return f"{preamble}{_USER_INTERJECTION_BODY_MARKER}{self.message}"


@dataclass(frozen=True)
class MetacognitiveAdvisory:
    """Advisory carrying a metacognitive nudge attached to a tool result.

    Used for nudges that respond to model behaviour at a tool boundary
    (``tool_error``, ``repeat``).  Nudges that respond to user behaviour
    (``correction``, ``denial``, ``resume``, ``start``, ``completion``)
    splice into the next user message instead, so they share the same
    ``<system-reminder>`` envelope but skip this advisory path.
    """

    nudge_type: str
    message: str

    @property
    def advisory_type(self) -> str:
        return f"metacognitive_{self.nudge_type}"

    def render(self) -> str:
        return self.message


# -- Wrapper ------------------------------------------------------------------


def escape_wrapper_tags(text: str) -> str:
    """Neutralise sequences that would break the advisory envelope.

    Replaces ``<tool_output>`` and ``<system-reminder>`` (open and close)
    with their HTML-entity-encoded forms so adjacent untrusted text
    cannot fabricate or close one of the wrapper blocks. Use this on any
    untrusted content that is glued next to a wrapper tag — tool output,
    user message bodies, and (defense-in-depth) advisory render output.

    Round-trip symmetry with :func:`history_decoration._entity_decode_wrapper_tags`
    requires escaping ``&`` first.  Otherwise a tool output that contains
    the literal string ``&lt;tool_output&gt;`` (e.g. documentation
    describing the wrapper format) would round-trip to the bare
    ``<tool_output>`` tag, fabricating an envelope the wrapper layer
    never produced.  The short-circuit on ``"<" not in text and "&" not
    in text`` covers the common case where neither wrapper tag nor any
    pre-existing entity is present — most tool outputs.
    """
    if "<" not in text and "&" not in text:
        return text
    # Encode ``&`` first so a pre-existing literal like ``&lt;tool_output&gt;``
    # in the source text becomes ``&amp;lt;tool_output&amp;gt;`` and
    # cannot collide with our wrapper-tag escape strings.
    return (
        text.replace("&", "&amp;")
        .replace("</tool_output>", "&lt;/tool_output&gt;")
        .replace("<tool_output>", "&lt;tool_output&gt;")
        .replace("<system-reminder>", "&lt;system-reminder&gt;")
        .replace("</system-reminder>", "&lt;/system-reminder&gt;")
    )


def wrap_tool_result(
    output: str,
    advisories: list[ToolAdvisory] | None = None,
) -> str:
    """Wrap tool output with advisory blocks when advisories are present.

    When *advisories* is empty or ``None`` the raw *output* is returned
    unchanged — no tags, no overhead.  Both the tool output and each
    advisory's render text are escaped before interpolation: a future
    caller wiring user-controlled text through the advisory layer
    cannot close the ``<system-reminder>`` envelope from inside.
    """
    if not advisories:
        return output

    parts = [f"<tool_output>\n{escape_wrapper_tags(output)}\n</tool_output>"]
    for advisory in advisories:
        parts.append(
            f"\n<system-reminder>\n{escape_wrapper_tags(advisory.render())}\n</system-reminder>"
        )
    return "\n".join(parts)


def render_system_reminder(text: str) -> str:
    """Render a standalone ``<system-reminder>`` block.

    For attaching out-of-band guidance to a non-tool message — currently
    the user-message metacognitive channel.  ``wrap_tool_result`` builds
    the same envelope inline for tool results; this helper exists so the
    user-message path uses the exact same envelope and escaping rules.
    """
    return f"<system-reminder>\n{escape_wrapper_tags(text)}\n</system-reminder>"


def parse_priority(text: str) -> tuple[str, str]:
    """Extract priority prefix from user message text.

    Returns ``(cleaned_text, priority)`` where *priority* is
    ``"important"`` if the message starts with ``!!!`` or ``"notice"``
    otherwise.
    """
    if text.startswith("!!!"):
        return text[3:].lstrip(), PRIORITY_IMPORTANT
    return text, PRIORITY_NOTICE
