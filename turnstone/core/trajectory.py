"""Canonical trajectory model — the provider-NEUTRAL typed ``Turn``.

This is the in-memory representation of a conversation turn that the wire-shape
refactor narrows onto: storage deserializes rows into ``Turn``s, the lowering layer
prepares them for a provider family, and the per-provider translators format them.
It is a *typing* of the message shape that already exists (OpenAI-like dicts plus the
``_``-prefixed side-channel keys), not a provider-specific shape — provider-typed
canonical would break cross-provider resume.

Field set and rationale: ``docs/design/canonical-trajectory-ideal-target.md`` §2.

NOTE: ``AttachmentRef`` references a content-addressed blob in ``workstream_attachments``
(the by-reference attachment model).  Wiring attachment content through ``Turn`` lands
with that storage cut; until then the model is defined here but the dict↔Turn adapters
cover text / tool_calls / native / tool turns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    """The four canonical roles.  ``developer`` collapses into ``SYSTEM`` at ingest
    (every consumer already treats them identically); operator-context turns are
    ``SYSTEM`` turns with a non-``None`` :attr:`Turn.source`."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


@dataclass(slots=True)
class TextBlock:
    """Portable text content."""

    text: str


@dataclass(slots=True)
class AttachmentRef:
    """A reference to attachment bytes held in the content-addressed blob store.

    Non-text content is carried *by reference* (never inline bytes): the translator
    resolves ``attachment_id`` to bytes and expands it to the provider's image /
    document format at wire time.  ``kind`` is ``"image"`` or ``"document"``.
    """

    attachment_id: str
    kind: str


ContentBlock = TextBlock | AttachmentRef


@dataclass(slots=True)
class ToolCall:
    """A client (locally-executed) tool call.  Server-side tool calls live in
    :class:`ProviderNative`, not here.  ``arguments`` stays the raw JSON string the
    model emitted, so it round-trips byte-exact (no re-serialization drift)."""

    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class ProviderNative:
    """The one opaque provider-native lane (reasoning, server-tool results, …).

    Replayed verbatim to the producing provider and dropped (rebuilt from the neutral
    fields) for any other.  ``blocks`` are opaque on the wire path and never inspected
    there; the UI display projection is the only reader that looks inside.
    """

    producer: str
    blocks: tuple[Any, ...] = ()


@dataclass(slots=True)
class TurnMeta:
    """Sidecar metadata: never reaches the wire, never read by the lowering layer.

    ``event_id`` is the per-ws SSE ``Last-Event-ID`` resume cursor; ``extra`` holds
    open operator-turn metadata (e.g. ``watch_name``)."""

    event_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Turn:
    """A single conversation turn — flat and role-discriminated.

    Role-specific fields are optional: ``tool_calls``/``native`` ride ``ASSISTANT``,
    ``tool_call_id``/``is_error`` ride ``TOOL``, ``source`` marks an operator-context
    ``SYSTEM`` turn.  Flat rather than a per-role union because every validity/lowering
    pass is a ``role`` switch-statement state machine; a union would force isomorphic
    ``isinstance`` ladders and fight the one-row-one-``Turn`` storage bridge.
    """

    role: Role
    content: tuple[ContentBlock, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    is_error: bool = False
    source: str | None = None
    native: ProviderNative | None = None
    meta: TurnMeta = field(default_factory=TurnMeta)

    @property
    def text(self) -> str:
        """The turn's text content — the FTS projection and the str fast-path.

        Joins the text of every :class:`TextBlock`; non-text blocks (attachments)
        contribute nothing (you cannot full-text-search an image)."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    # -- construction helpers (blunt the wrapping cost of uniform block content) --
    @classmethod
    def user(cls, text: str) -> Turn:
        return cls(Role.USER, (TextBlock(text),))

    @classmethod
    def assistant(
        cls,
        text: str = "",
        *,
        tool_calls: tuple[ToolCall, ...] = (),
        native: ProviderNative | None = None,
    ) -> Turn:
        return cls(
            Role.ASSISTANT,
            (TextBlock(text),) if text else (),
            tool_calls=tool_calls,
            native=native,
        )

    @classmethod
    def tool(cls, tool_call_id: str, text: str, *, is_error: bool = False) -> Turn:
        return cls(
            Role.TOOL,
            (TextBlock(text),) if text else (),
            tool_call_id=tool_call_id,
            is_error=is_error,
        )

    @classmethod
    def system(cls, text: str, *, source: str | None = None) -> Turn:
        return cls(Role.SYSTEM, (TextBlock(text),), source=source)
