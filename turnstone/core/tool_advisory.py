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
    """Advisory for a message the user sent while the model was executing."""

    message: str
    priority: str = PRIORITY_NOTICE

    @property
    def advisory_type(self) -> str:
        return "user_interjection"

    def render(self) -> str:
        if self.priority == PRIORITY_IMPORTANT:
            preamble = (
                "The user sent a message while you were working. "
                "You MUST address this before continuing."
            )
        else:
            preamble = (
                "The user sent additional context while you were working. "
                "Incorporate if relevant, otherwise continue."
            )
        return f"{preamble}\n\nUser message: {self.message}"


# -- Wrapper ------------------------------------------------------------------


def wrap_tool_result(
    output: str,
    advisories: list[ToolAdvisory] | None = None,
) -> str:
    """Wrap tool output with advisory blocks when advisories are present.

    When *advisories* is empty or ``None`` the raw *output* is returned
    unchanged — no tags, no overhead.
    """
    if not advisories:
        return output

    parts = [f"<tool_output>\n{output}\n</tool_output>"]
    for advisory in advisories:
        parts.append(f"\n<system-reminder>\n{advisory.render()}\n</system-reminder>")
    return "\n".join(parts)


def parse_priority(text: str) -> tuple[str, str]:
    """Extract priority prefix from user message text.

    Returns ``(cleaned_text, priority)`` where *priority* is
    ``"important"`` if the message starts with ``!!!`` or ``"notice"``
    otherwise.
    """
    if text.startswith("!!!"):
        return text[3:].lstrip(), PRIORITY_IMPORTANT
    return text, PRIORITY_NOTICE
