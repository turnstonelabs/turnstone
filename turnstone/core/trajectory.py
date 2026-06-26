"""Canonical trajectory model — the provider-NEUTRAL typed ``Turn``.

This is the in-memory representation of a conversation turn that the wire-shape
refactor narrows onto: storage deserializes rows into ``Turn``s, the lowering layer
prepares them for a provider family, and the per-provider translators format them.
It is a *typing* of the message shape that already exists (OpenAI-like dicts plus the
``_``-prefixed side-channel keys), not a provider-specific shape — provider-typed
canonical would break cross-provider resume.

Field set and rationale: ``docs/design/canonical-trajectory-ideal-target.md`` §2.

NOTE: non-text content rides as ``AttachmentRef`` — a reference to a content-addressed
blob in ``workstream_attachments``.  ``Turn``s never carry bytes, and the dict bridge
carries only the ``{type: kind, attachment_id}`` placeholder.  Each output boundary (the
provider translator, the ``/history`` display, export) materializes the placeholder to an
inline part by point-lookup against the blob store, via :func:`resolve_attachment_parts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class Role(StrEnum):
    """The four canonical roles.  ``developer`` collapses into ``SYSTEM`` at ingest
    (every consumer already treats them identically); operator-context turns are
    ``SYSTEM`` turns with a non-``None`` :attr:`Turn.source`."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class EffectStatus(StrEnum):
    """The typed disposition of a tool call's *effect* — the machine-readable
    twin of the prose the result body already carries.

    Rides ``TurnMeta.extra["effect_status"]`` (wire-invisible, like the other
    side-channel meta): the *model* reads the body, while *deterministic*
    consumers — a re-issue guard, owner-side compensation — read this. The
    ``unknown`` vs ``none`` split is the load-bearing one (HYPOTHESIS.md
    effect-record appendix: *unknown, never none*): an unobserved outcome must
    not read as "did not happen." ``None`` (the field unset) means an ordinary
    result that no producer classified — not a fourth status.
    """

    COMMITTED = "committed"  # ran to completion; effects, if any, landed
    NONE = "none"  # definitively did nothing (denied / never started)
    UNKNOWN = "unknown"  # stopped mid-flight; may or may not have acted
    PARTIAL = "partial"  # ran part-way
    ROLLED_BACK = "rolled_back"  # ran, then reverted


@dataclass(slots=True)
class TextBlock:
    """Portable text content."""

    text: str


@dataclass(slots=True)
class AttachmentRef:
    """A reference to attachment bytes held in the content-addressed blob store.

    Non-text content is carried *by reference* (never inline bytes): the translator
    resolves ``attachment_id`` to bytes and expands it to the provider's native
    format at wire time.  ``kind`` is the by-reference placeholder type —
    ``"image"``, ``"document"`` (text docs), ``"pdf"``, or ``"audio"``.  The
    dict-bridge keys off ``attachment_id`` and is kind-agnostic, so new kinds
    need no change here.
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
    open metadata under well-known keys — ``"source_meta"`` (an operator-context
    ``system`` turn's structured per-kind fields, e.g. ``watch_triggered``'s
    ``watch_name`` / ``command`` / poll counters; persisted in the
    ``conversations.meta`` column, surfaced to the FE for per-kind rendering) and
    ``"attachments_meta"`` (display metadata for by-reference attachments)."""

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

    @property
    def effect_status(self) -> EffectStatus | None:
        """The typed effect disposition recorded on a TOOL turn, or ``None`` if
        unset (an ordinary, unclassified result). Read by deterministic
        consumers; never sent to the model. Lenient on a corrupt stored value
        (returns ``None`` rather than raising), mirroring the meta decoders."""
        raw = self.meta.extra.get("effect_status")
        if not raw:
            return None
        try:
            return EffectStatus(raw)
        except ValueError:
            return None

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
    def tool(
        cls,
        tool_call_id: str,
        text: str,
        *,
        is_error: bool = False,
        effect_status: EffectStatus | None = None,
    ) -> Turn:
        meta = TurnMeta()
        if effect_status is not None:
            meta.extra["effect_status"] = effect_status.value
        return cls(
            Role.TOOL,
            (TextBlock(text),) if text else (),
            tool_call_id=tool_call_id,
            is_error=is_error,
            meta=meta,
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
            elif isinstance(part, dict) and part.get("attachment_id"):
                # The by-reference placeholder ``{type: kind, attachment_id}`` —
                # the canonical form for non-text content (bytes resolve at the
                # translator).  A *resolved* inline part never reaches here:
                # resolution is terminal (the wire payload / display output), so
                # such a part is dropped rather than carried as bytes.
                blocks.append(
                    AttachmentRef(attachment_id=part["attachment_id"], kind=part.get("type", ""))
                )
        return tuple(blocks)
    return ()  # unexpected scalar — drop


def _content_to_raw(content: tuple[ContentBlock, ...]) -> str | list[dict[str, Any]]:
    """Inverse of :func:`_content_from_raw`.

    A single text block collapses to a plain string (the 95% case, and what an
    empty turn round-trips to); multiple blocks — or any non-text block — take
    the multipart list form.  The multiple-block rule preserves an all-*text*
    multipart list (the unreadable-attachment placeholder path emits one) instead
    of collapsing it to a joined string.
    """
    if not content:
        return ""
    if len(content) == 1 and isinstance(content[0], TextBlock):
        return content[0].text
    parts: list[dict[str, Any]] = []
    for b in content:
        if isinstance(b, TextBlock):
            parts.append({"type": "text", "text": b.text})
        elif isinstance(b, AttachmentRef):
            # By-reference placeholder; the translator (or reconstruct) resolves
            # it to an inline part via :func:`resolve_attachment_parts`.
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
    # Operator-context per-kind structured fields (``watch_triggered`` etc.).
    # Carried as ONE dict so it maps to one ``conversations.meta`` column / one
    # FE ``meta`` field, rather than scattered ``_``-prefixed siblings.
    sm = msg.get("_source_meta")
    if sm:
        meta.extra["source_meta"] = sm
    # Typed tool-effect disposition (wire-invisible side channel, like the above).
    es = msg.get("_effect_status")
    if es:
        meta.extra["effect_status"] = es

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
    sm = turn.meta.extra.get("source_meta")
    if sm:
        msg["_source_meta"] = sm
    es = turn.meta.extra.get("effect_status")
    if es:
        msg["_effect_status"] = es
    return msg


def turns_from_dicts(msgs: list[dict[str, Any]]) -> list[Turn]:
    return [turn_from_dict(m) for m in msgs]


def dicts_from_turns(turns: list[Turn]) -> list[dict[str, Any]]:
    return [turn_to_dict(t) for t in turns]


def resolve_attachment_parts(
    messages: list[dict[str, Any]], parts_by_id: dict[str, Any]
) -> list[dict[str, Any]]:
    """Replace by-reference attachment placeholders with resolved inline parts.

    The by-reference content lane reaches the wire (and ``/history`` display) as
    ``{type: kind, attachment_id}`` placeholders in a message's list content;
    *parts_by_id* maps an id to its inline content part — or a *list* of parts
    (one placeholder may expand to several, e.g. a PDF rasterized to one image
    per page for a vision model) — built from the content-addressed blob.  This is the materialization the
    translator — and reconstruct, for display — runs at its output boundary: a
    placeholder whose blob is missing (pruned) is dropped, so a consumer never
    sees an unresolved reference.  Identity-preserving when no message carries a
    placeholder; never mutates the input.
    """

    def _refs(content: Any) -> bool:
        return isinstance(content, list) and any(
            isinstance(p, dict) and p.get("attachment_id") for p in content
        )

    if not any(_refs(m.get("content")) for m in messages):
        return messages
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list) or not _refs(content):
            out.append(m)
            continue
        new_parts: list[Any] = []
        for p in content:
            if isinstance(p, dict) and p.get("attachment_id"):
                resolved = parts_by_id.get(str(p["attachment_id"]))
                if isinstance(resolved, list):
                    # One placeholder → several parts (e.g. a PDF rasterized to
                    # one image per page for a vision model).
                    new_parts.extend(resolved)
                elif resolved is not None:
                    new_parts.append(resolved)
                # else: pruned blob — drop the placeholder.
            else:
                new_parts.append(p)
        out.append({**m, "content": new_parts})
    return out


def materialize_attachments(
    messages: list[dict[str, Any]],
    resolve: Callable[[list[str]], dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Expand by-reference attachment placeholders to inline parts at the wire.

    The translator's entry point for the by-reference content lane: collect the
    placeholder ids across *messages*, ask *resolve* (a storage point-lookup the
    session hands down) for their inline content parts, and substitute via
    :func:`resolve_attachment_parts`.  A ``None`` resolver (no storage — e.g. a
    unit test or an in-memory sub-agent whose media is already inline) or a
    placeholder-free trajectory is a no-op, so the common path is allocation-free.
    """
    if resolve is None:
        return messages
    ids = sorted(
        {
            str(p["attachment_id"])
            for m in messages
            if isinstance(m.get("content"), list)
            for p in m["content"]
            if isinstance(p, dict) and p.get("attachment_id")
        }
    )
    if not ids:
        return messages
    return resolve_attachment_parts(messages, resolve(ids))
