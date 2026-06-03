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


@dataclass(slots=True)
class RawContentBlock:
    """Transitional carrier for a verbatim wire content-part dict (``image_url`` /
    ``document``).

    The by-reference model (§2/§6) is :class:`AttachmentRef` — id + kind, with the
    translator resolving bytes at wire time.  Until that wiring lands (it relocates
    byte-resolution out of ``reconstruct``'s inline data-URL path), the dict↔Turn
    adapters carry an attachment part *verbatim* here so multipart turns round-trip
    byte-identically.  Contributes nothing to :attr:`Turn.text` (you cannot
    full-text-search an image).  Removed when :class:`AttachmentRef` is wired through.
    """

    part: dict[str, Any]


ContentBlock = TextBlock | AttachmentRef | RawContentBlock


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


# --------------------------------------------------------------------------- #
# dict ↔ Turn adapters (the strangler bridge).
#
# The wire/storage path is OpenAI-like dicts plus ``_``-prefixed side channels.
# These adapters are a lossless, byte-identical bridge so the migration to
# ``Turn`` can proceed one boundary at a time: a layer that produces dicts can be
# read as ``Turn``s, and a layer that holds ``Turn``s can hand dicts to a
# not-yet-migrated consumer.  ``turn_to_dict(turn_from_dict(d)) == d`` for every
# shape ``reconstruct_messages`` / the wire path produces (see test_trajectory).
# --------------------------------------------------------------------------- #
def _content_from_raw(raw: Any) -> tuple[ContentBlock, ...]:
    """Map a dict ``content`` value to typed content blocks."""
    if not raw:  # None / "" / [] → empty (reconstruct emits "" for empty turns)
        return ()
    if isinstance(raw, str):
        return (TextBlock(raw),)
    if isinstance(raw, list):
        blocks: list[ContentBlock] = []
        for part in raw:
            if isinstance(part, dict) and part.get("type") == "text":
                blocks.append(TextBlock(part.get("text", "")))
            else:
                blocks.append(RawContentBlock(part))
        return tuple(blocks)
    return (RawContentBlock(raw),)  # defensive — unexpected scalar


def _content_to_raw(content: tuple[ContentBlock, ...]) -> str | list[dict[str, Any]]:
    """Inverse of :func:`_content_from_raw`.

    All-text content collapses to a plain string (the 95% case, and what an empty
    turn round-trips to); any non-text block forces the multipart list form.
    """
    if not content:
        return ""
    if all(isinstance(b, TextBlock) for b in content):
        return "".join(b.text for b in content if isinstance(b, TextBlock))
    parts: list[dict[str, Any]] = []
    for b in content:
        if isinstance(b, TextBlock):
            parts.append({"type": "text", "text": b.text})
        elif isinstance(b, RawContentBlock):
            parts.append(b.part)
        elif isinstance(b, AttachmentRef):  # not produced pre-by-ref-wiring; defensive
            parts.append({"type": b.kind, "attachment_id": b.attachment_id})
    return parts


def turn_from_dict(msg: dict[str, Any]) -> Turn:
    """Read an OpenAI-like message dict (with ``_``-side channels) as a ``Turn``."""
    role_str = msg.get("role", "")
    # ``developer`` collapses into SYSTEM (zero writers; providers treat them
    # identically, so the wire is unaffected by the normalization).
    role = Role.SYSTEM if role_str in ("system", "developer") else Role(role_str)

    tool_calls = tuple(
        ToolCall(
            id=tc.get("id", ""),
            name=tc.get("function", {}).get("name", ""),
            arguments=tc.get("function", {}).get("arguments", ""),
        )
        for tc in (msg.get("tool_calls") or [])
    )

    native: ProviderNative | None = None
    pc = msg.get("_provider_content")
    if pc is not None:
        native = ProviderNative(producer=msg.get("_producer", ""), blocks=tuple(pc))

    meta = TurnMeta(event_id=msg.get("_event_id"))
    am = msg.get("_attachments_meta")
    if am is not None:
        meta.extra["attachments_meta"] = am

    return Turn(
        role=role,
        content=_content_from_raw(msg.get("content")),
        tool_calls=tool_calls,
        tool_call_id=msg.get("tool_call_id"),
        is_error=bool(msg.get("is_error", False)),
        source=msg.get("_source"),
        native=native,
        meta=meta,
    )


def turn_to_dict(turn: Turn) -> dict[str, Any]:
    """Render a ``Turn`` back to the OpenAI-like dict shape (inverse of above)."""
    msg: dict[str, Any] = {"role": turn.role.value, "content": _content_to_raw(turn.content)}
    if turn.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in turn.tool_calls
        ]
    if turn.tool_call_id is not None:
        msg["tool_call_id"] = turn.tool_call_id
    if turn.is_error:
        msg["is_error"] = True
    if turn.source is not None:
        msg["_source"] = turn.source
    if turn.native is not None:
        msg["_provider_content"] = list(turn.native.blocks)
        if turn.native.producer:
            msg["_producer"] = turn.native.producer
    if turn.meta.event_id is not None:
        msg["_event_id"] = turn.meta.event_id
    am = turn.meta.extra.get("attachments_meta")
    if am is not None:
        msg["_attachments_meta"] = am
    return msg


def turns_from_dicts(msgs: list[dict[str, Any]]) -> list[Turn]:
    return [turn_from_dict(m) for m in msgs]


def dicts_from_turns(turns: list[Turn]) -> list[dict[str, Any]]:
    return [turn_to_dict(t) for t in turns]
