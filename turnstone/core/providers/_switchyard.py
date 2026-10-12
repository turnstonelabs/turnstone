"""Switchyard provider adapter — the execution boundary for Switchyard's lanes.

Responsibility split (maintainer direction, 2026-09-21 — ``Turn IR -> lowering
-> provider``): Turnstone owns ledger/session truth, resumed-turn and tool-round
semantics, reasoning/replay state, whether reasoning material exists at all,
whether that material is transferable to another provider, the information-loss
classification and the provider-neutral execution requirements.  Switchyard owns
endpoint/model selection, fleet readiness, local/cloud topology, cost,
escalation, thinking controls on the selected endpoint and GPU/model
availability.

The adapter therefore decides nothing about routing.  It lowers the neutral
ledger into the request Switchyard's surface accepts and states what had to be
left behind when reasoning material cannot cross the provider boundary.

Cross-provider reasoning rule
-----------------------------
A Switchyard lane is one provider boundary.  An item that lane's own surface
returned is that provider's object, so it crosses back whole: its id and its
encrypted payload are the provider's own replay state, resolvable only at the
endpoints behind that boundary, and those are the endpoints being called.  Removing
them would cost the replay without protecting anything, so a native item is handed
on untouched and the surface's own projection decides what the wire carries.  An
item attributed to a different producer is not that provider's object: it does not
cross as native, and it is reported as dropped rather than forwarded carrying a
foreign binding.  An item with no handle to build the surface's item from is
dropped as well, since that surface's projection would not emit it whatever the
block held.  Nothing is substituted for what does not cross.

What the boundary does depends on the surface, because the two surfaces do not
represent reasoning the same way.  A Responses surface carries the reasoning item
itself, bindings and all, and the parent's own projection builds one only from what
the item already holds (a string ``id``, plus whatever ``summary``/``content``/
``encrypted_content`` arrived with it) -- so a native item passes through with
nothing removed, and ``_openai_responses._reasoning_item_for_input`` returning
``None`` for want of a string ``id`` is reported as a drop, because no item is
emitted for it at all.  A Chat surface has no reasoning-item representation at all
-- its wire carries the readable text through the canonical message field and
sanitization drops the private block list -- so a binding that wire could never have
carried is removed and costs nothing, and the block is reported as kept.
``_SURFACE_CARRIERS`` states this per surface.

Synthetic round repair is deliberately not part of this provider.  The provider does
not fabricate missing reasoning or opaque replay state; replay support and
missing-history repair are separate questions, and a commercial Responses endpoint
rejects a fabricated round item.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

    from turnstone.core.providers._protocol import StreamChunk

PROVIDER_NAME = "switchyard"

# ─── Information-loss classification ────────────────────────────────────────
# Switchyard's own translation codecs decode a Responses reasoning item for its text
# -- ``content``, ``summary`` and a top-level ``text`` (see
# ``crates/switchyard-translation/src/codecs/responses/buffered.rs:649``) -- and carry
# no binding into the internal ``ContentBlock::Reasoning``.  That describes the
# codec's internal shape, not what the lane's Responses surface does end to end: a
# live turn on a DeepSeek-backed lane returns items carrying ``id`` and
# ``encrypted_content`` and accepts them back unchanged on the resumed round, so the
# binding is this provider's own replay state and belongs on the wire.  Text is the
# part a Chat wire carries; an item is the part a Responses wire carries.

LOSS_FOREIGN_ENCRYPTED = "foreign_encrypted_binding_dropped"
LOSS_NATIVE_SIGNED = "native_signed_binding_dropped"
LOSS_UNREPRESENTABLE = "reasoning_content_unrepresentable"
# The surface's own projection cannot build an item from this block, so no item is
# emitted and the block does not cross -- reporting it as carried would name a
# crossing the wire never performed.
LOSS_ITEM_UNREPRESENTABLE = "reasoning_item_unrepresentable_without_binding"
# The calling parent projects native blocks only for the producer that recorded them
# (``_openai_responses._convert_messages`` keeps a block whose ``_producer`` is absent
# or equal to the surface's ``native_producer``), so a block recorded on another lane
# is never read, whatever its shape.
LOSS_FOREIGN_PRODUCER = "reasoning_item_ignored_for_foreign_producer"

# Reasoning block shapes whose text this adapter knows how to lower.
_REASONING_BLOCK_TYPES = ("reasoning", "reasoning_text", "thinking")
# The two ways a block stops being plain text, checked in this order: a signed
# item and a foreign-encrypted item.
_SIGNATURE_FIELDS = ("signature", "thinking_signature")
_ENCRYPTED_FIELDS = ("encrypted_content", "encrypted_reasoning")
# Everything removed from a bound block before its text crosses.  The item id sits
# here with the blobs: it is the handle that resolves a stored item at the endpoint
# that issued it, so a different endpoint cannot dereference it either.
_BINDING_FIELDS = (*_SIGNATURE_FIELDS, *_ENCRYPTED_FIELDS, "id")


@dataclass(frozen=True)
class _SurfaceCarrier:
    """What a surface's wire needs for reasoning material to be representable on it.

    ``required_bindings`` are the string handles that surface's projection reads to
    build an item.  ``native_producer`` is the producer name whose native blocks that
    surface's parent will project, or ``None`` where the parent does not select native
    blocks by producer at all.  ``binding_is_carried`` says whether the wire carries
    binding metadata at all: where it does, a native block is the provider's own
    object and crosses untouched, and where it does not, a binding is removed because
    that wire could never have carried it.
    """

    required_bindings: tuple[str, ...]
    native_producer: str | None
    binding_is_carried: bool


_SURFACE_CARRIERS: dict[str, _SurfaceCarrier] = {
    # Responses carries the reasoning item itself, bindings included, and it is the
    # surface the provider that issued the item is reached through, so a native item
    # crosses whole.  The handle it needs is the one its own projection reads; a block
    # without it is a drop, since no item is emitted for it.
    "responses": _SurfaceCarrier(
        required_bindings=("id",), native_producer=PROVIDER_NAME, binding_is_carried=True
    ),
    # Chat carries the readable text in the canonical message field and never
    # projects the private block list, so it needs no binding and can lose none.
    "chat": _SurfaceCarrier(required_bindings=(), native_producer=None, binding_is_carried=False),
}


def _is_representable(block: dict[str, Any], required_bindings: tuple[str, ...]) -> bool:
    """Whether this surface's projection can still build an item from *block*.

    Mirrors the parent's own requirement: it reads a string handle per required
    binding and yields nothing when one is missing, empty, or of the wrong type, so a
    non-string value is not representable either.
    """
    for field in required_bindings:
        value = block.get(field)
        if not isinstance(value, str) or not value:
            return False
    return True


def _plaintext_of(block: dict[str, Any]) -> str:
    """Return the plain reasoning text carried by *block*, or ``""``.

    Reads the same fields Switchyard's own decoder reads, so what this adapter
    considers transferable is what the receiving codec will actually pick up.
    """
    for key in ("reasoning_text", "text", "thinking"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    parts: list[str] = []
    for key in ("summary", "content"):
        for part in block.get(key) or []:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "".join(parts)


def lower_reasoning_block(block: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return what may cross to another provider's endpoint, and what that costs.

    ``(block, None)`` when the block crosses unchanged, ``(lowered, loss)`` when it
    crosses with its binding removed, and ``(None, loss)`` when nothing readable is
    left.  ``(None, None)`` means the block held no reasoning text: nothing to carry
    is not the same as something lost, so it is neither kept nor reported.

    Bindings are checked signature-first, so a block bound both ways is reported
    once under the signature class; every binding field is removed either way.
    """
    text = _plaintext_of(block)
    signed = any(block.get(field) for field in _SIGNATURE_FIELDS)
    encrypted = any(block.get(field) for field in _ENCRYPTED_FIELDS)
    if signed or encrypted:
        if not text:
            # The binding is the reasoning: no endpoint but the issuer's can resolve
            # the blob or check the signature, so there is nothing readable to carry.
            return None, LOSS_UNREPRESENTABLE
        lowered = {key: value for key, value in block.items() if key not in _BINDING_FIELDS}
        return lowered, LOSS_NATIVE_SIGNED if signed else LOSS_FOREIGN_ENCRYPTED
    if not text:
        return None, None
    return block, None


@dataclass
class ReasoningTransfer:
    """Outcome of one boundary crossing: what crossed and what it cost."""

    kept: int = 0
    """Blocks whose reasoning material this surface's wire carries.

    Readable text where the surface carries reasoning as text, and the provider's own
    opaque item where the block is that provider's object and crosses back whole.
    """
    stripped: int = 0
    """Blocks that still cross with binding metadata removed.

    No surface reaches this: a wire that carries binding metadata hands the item back
    to the provider that issued it, so a native block crosses with nothing removed,
    while a wire with no binding field never carried the binding to begin with.  The
    field stays in the report so a caller reading the schema does not have to tell a
    surface that cannot strip apart from a surface that has not stripped yet.
    """
    dropped: int = 0
    """Blocks whose reasoning material this surface's wire does not receive."""
    losses: tuple[str, ...] = ()
    """Distinct loss classes, in the order they were first seen."""

    @property
    def lossy(self) -> bool:
        return bool(self.losses)


def retain_transferable_reasoning(
    messages: list[dict[str, Any]],
    *,
    required_bindings: tuple[str, ...],
    native_producer: str | None,
    binding_is_carried: bool,
) -> tuple[list[dict[str, Any]], ReasoningTransfer]:
    """Rebuild *messages* with only what this boundary can carry.

    Returns ``(messages, transfer)``.  The list and any message it changes are new
    objects: a caller's ledger objects stay untouched, including the blocks that were
    lowered.  Only ``_provider_content`` is inspected, and only blocks whose type is a
    recognised reasoning shape; canonical ``content``, ``tool_calls`` and any block
    this adapter does not interpret pass through unchanged, so the receiving provider
    is never handed a rewritten tool history.

    *required_bindings*, *native_producer* and *binding_is_carried* are the surface
    being lowered for (see ``_SURFACE_CARRIERS``) and they decide what the boundary
    does with each block.  A message whose native blocks the calling parent will not
    project at all -- recorded by a producer other than the surface's own -- holds
    nothing that reaches the wire, so its blocks are dropped whole and none of them is
    lowered on the way: a foreign opaque binding is not re-emitted in any form.  On a
    surface whose wire carries binding metadata, a native block is that provider's own
    object and crosses untouched, so nothing is removed and no loss is recorded; the
    only thing that keeps such a block off the wire is that surface's projection being
    unable to build its item, which is a drop.  On a surface with no binding field at
    all, the binding is removed because that wire could never have carried it, the
    readable text crosses, and the removal is not a cost.
    """
    out: list[dict[str, Any]] = []
    kept = stripped = dropped = 0
    losses: list[str] = []

    def _note(loss: str | None) -> None:
        if loss and loss not in losses:
            losses.append(loss)

    for message in messages:
        blocks = message.get("_provider_content")
        if not isinstance(blocks, list) or not blocks:
            out.append(message)
            continue
        producer = message.get("_producer")
        foreign_producer = bool(
            native_producer is not None and producer and producer != native_producer
        )
        surviving: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") not in _REASONING_BLOCK_TYPES:
                surviving.append(block)
                continue
            if foreign_producer:
                # The parent projects native blocks only for the producer that recorded
                # them, so this block reaches the wire in no shape at all.  It is
                # dropped rather than lowered: a foreign opaque binding is not
                # re-emitted, in this list or any list built from it.
                dropped += 1
                _note(LOSS_FOREIGN_PRODUCER)
                continue
            if binding_is_carried:
                # This wire carries binding metadata, and the provider it reaches is
                # the one that issued the block: the block is that provider's own
                # replay state and crosses untouched.  Whether the wire carries it is
                # its projection's decision, so a block no item can be built from is
                # dropped rather than dismantled in the hope of one.
                if _is_representable(block, required_bindings):
                    surviving.append(block)
                    kept += 1
                else:
                    dropped += 1
                    _note(LOSS_ITEM_UNREPRESENTABLE)
                continue
            lowered, loss = lower_reasoning_block(block)
            if lowered is None and loss is None:
                continue  # nothing to carry, so nothing is lost either
            if lowered is None:
                dropped += 1
                _note(loss)
                continue
            if not _is_representable(lowered, required_bindings):
                dropped += 1
                _note(LOSS_ITEM_UNREPRESENTABLE)
                continue
            # No binding field exists on this surface, so a binding it could never
            # have carried was not taken from it: the readable text crosses.
            surviving.append(lowered)
            kept += 1
        if surviving:
            out.append({**message, "_provider_content": surviving})
        else:
            # Nothing was left to carry: drop the private list rather than leave an
            # empty one, so the lowering stage sees the shape it sees for a message
            # that never carried reasoning.
            out.append({key: value for key, value in message.items() if key != "_provider_content"})
    return out, ReasoningTransfer(
        kept=kept, stripped=stripped, dropped=dropped, losses=tuple(losses)
    )


# ─── Provider ───────────────────────────────────────────────────────────────


class _SwitchyardBoundary:
    """The crossing every Switchyard surface shares.

    Concrete adapters override ``create_streaming`` and call ``_lower_messages``
    first.  The split keeps one implementation of the crossing while leaving each
    surface's ``create_streaming`` a real override of the parent it inherits from,
    so a surface cannot bypass the classification by accident: skipping the call
    means handing the parent unfiltered reasoning, which the surface tests catch.

    The crossing outcome is published on ``last_reasoning_transfer``, held per
    calling thread rather than on the adapter: the factory hands out one shared
    instance per surface, so a second session crossing while the first is still
    between its call and its read would otherwise overwrite the first session's
    counts.  Every crossing happens and is read on the thread that drives the
    turn, which is what makes a thread-local slot the right scope.
    """

    _switchyard_surface = "responses"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._crossing_slot = threading.local()

    @property
    def provider_name(self) -> str:
        return PROVIDER_NAME

    @property
    def last_reasoning_transfer(self) -> ReasoningTransfer:
        """The calling thread's most recent crossing, empty when it has none."""
        return getattr(self._crossing_slot, "transfer", ReasoningTransfer())

    def _lower_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the list this surface should receive, recording what it cost.

        The outcome lands on ``self.last_reasoning_transfer`` (this thread's slot)
        for the caller to record alongside the turn.  The surface named by
        ``_switchyard_surface`` supplies the carrier policy the classification is
        read against; every surface the factory can build has a row.
        """
        carrier = _SURFACE_CARRIERS[self._switchyard_surface]
        lowered, transfer = retain_transferable_reasoning(
            messages,
            required_bindings=carrier.required_bindings,
            native_producer=carrier.native_producer,
            binding_is_carried=carrier.binding_is_carried,
        )
        self._crossing_slot.transfer = transfer
        return lowered


class SwitchyardResponsesProvider(_SwitchyardBoundary, OpenAIResponsesProvider):
    """Switchyard's Responses surface (``api_surface="responses"``).

    Operator-owned capabilities: Switchyard declares what its fleet serves, and a
    commercial capability table does not apply to a lane the operator owns.  That
    is the parent's ``compat`` mode, kept as the default here.
    """

    _switchyard_surface = "responses"

    def __init__(self, *, compat: bool = True) -> None:
        super().__init__(compat=compat)

    def create_streaming(
        self, *, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Iterator[StreamChunk]:
        # ``**kwargs`` rather than a copy of the parent's parameter list: the
        # crossing has no opinion about the rest of the request, and a duplicated
        # signature would go stale the first time the parent gains a parameter.
        return super().create_streaming(messages=self._lower_messages(messages), **kwargs)


class SwitchyardChatProvider(_SwitchyardBoundary, OpenAIChatCompletionsProvider):
    """Switchyard's Chat Completions surface (``api_surface="chat"``).

    The surface the production lanes ride today; the crossing is identical to the
    Responses class, so a lane can move between surfaces without changing what the
    ledger can carry.
    """

    _switchyard_surface = "chat"

    def __init__(self) -> None:
        super().__init__()

    def create_streaming(
        self, *, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Iterator[StreamChunk]:
        return super().create_streaming(messages=self._lower_messages(messages), **kwargs)
