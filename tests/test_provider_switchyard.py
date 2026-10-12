"""Boundary tests for the Switchyard provider.

The provider owns no routing decision: it lowers Turnstone's neutral ledger onto
Switchyard's surface and reports what the ledger could not carry.  What these
tests pin: the provider identity and surface selection, operator-owned
capabilities, and the provider-boundary reasoning rule -- an item the lane's own
surface produced is that provider's object and crosses back whole, bindings
included, while an item attributed to another producer does not cross as native and
is reported as dropped, nothing is substituted for what does not cross, and the
caller's ledger objects are never touched.
"""

from __future__ import annotations

import copy
import json
import threading
from typing import Any

import pytest

from turnstone.core.providers import create_provider
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_common import OPENAI_COMPAT_DEFAULT
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
from turnstone.core.providers._switchyard import (
    _SURFACE_CARRIERS,
    LOSS_FOREIGN_ENCRYPTED,
    LOSS_FOREIGN_PRODUCER,
    LOSS_ITEM_UNREPRESENTABLE,
    LOSS_NATIVE_SIGNED,
    LOSS_UNREPRESENTABLE,
    PROVIDER_NAME,
    ReasoningTransfer,
    SwitchyardChatProvider,
    SwitchyardResponsesProvider,
    lower_reasoning_block,
    retain_transferable_reasoning,
)

_PLAINTEXT = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "because"}]}
_CONTENT_PLAINTEXT = {
    "type": "reasoning",
    "content": [{"type": "reasoning_text", "text": "because"}],
}
_ENCRYPTED = {
    "type": "reasoning",
    "id": "rs_abc",
    "encrypted_content": "enc-blob",
    "summary": [{"type": "summary_text", "text": "because"}],
}
_SIGNED = {"type": "thinking", "thinking": "because", "signature": "sig-blob"}
# A reasoning item as a live DeepSeek-backed Switchyard lane returns it: a handle, a
# readable reasoning_text part, an empty summary and an encrypted payload.  Captured
# from a real turn rather than written by hand, because the hand-written shapes this
# file started with were not what the lane emits.
_LIVE_ITEM = {
    "type": "reasoning",
    "id": "84bd62f9-2e89-4fb2-84fe-a401cd68782c",
    "status": "completed",
    "content": [{"type": "reasoning_text", "text": "because"}],
    "summary": [],
    "encrypted_content": "0441e4bc-f2e1-478b-9dc7-6a12268b59db-0",
}
_TOOL_CALL = {"type": "function_call", "call_id": "call_1", "name": "get_weather"}
_HANDLED = {
    "type": "reasoning",
    "id": "rs_live",
    "summary": [{"type": "summary_text", "text": "why"}],
}

# The carrier policies the surfaces declare, spelled out so a test states which
# surface it is lowering for rather than inheriting one from the module.
_AS_CHAT: dict[str, Any] = {
    "required_bindings": (),
    "native_producer": None,
    "binding_is_carried": False,
}
_AS_RESPONSES: dict[str, Any] = {
    "required_bindings": ("id",),
    "native_producer": PROVIDER_NAME,
    "binding_is_carried": True,
}


class TestRegistration:
    def test_switchyard_shares_the_openai_compatible_surface_default(self) -> None:
        """An omitted surface means chat here, exactly as for openai-compatible.

        The lanes this provider is for declare an explicit ``api_surface``; the
        default exists for callers that pass nothing, and it must not differ from
        the wire family it replaces, or swapping a live row onto this provider
        would silently change the request shape.
        """
        assert isinstance(create_provider(PROVIDER_NAME), SwitchyardChatProvider)
        assert isinstance(create_provider(PROVIDER_NAME), OpenAIChatCompletionsProvider)
        # The sibling name resolves the same way, which is the contract being mirrored.
        assert isinstance(create_provider("openai-compatible"), OpenAIChatCompletionsProvider)
        assert isinstance(
            create_provider(PROVIDER_NAME, api_surface="responses"), SwitchyardResponsesProvider
        )

    def test_responses_surface_is_selectable(self) -> None:
        provider = create_provider(PROVIDER_NAME, api_surface="responses")
        assert isinstance(provider, SwitchyardResponsesProvider)

    def test_both_surfaces_report_the_switchyard_identity(self) -> None:
        assert create_provider(PROVIDER_NAME).provider_name == PROVIDER_NAME
        assert create_provider(PROVIDER_NAME, api_surface="chat").provider_name == PROVIDER_NAME
        assert (
            create_provider(PROVIDER_NAME, api_surface="responses").provider_name == PROVIDER_NAME
        )

    def test_unknown_surface_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unknown api_surface"):
            create_provider(PROVIDER_NAME, api_surface="grpc")

    def test_capabilities_are_operator_owned_not_commercial(self) -> None:
        """Switchyard declares its own fleet; no commercial capability table applies.

        The commercial name is deliberately a model this repo has a table row for:
        if Switchyard ever fell through to that table it would start imposing
        vendor quirks on lanes the operator owns.
        """
        for surface in (None, "chat", "responses"):
            provider = (
                create_provider(PROVIDER_NAME)
                if surface is None
                else create_provider(PROVIDER_NAME, api_surface=surface)
            )
            assert provider.get_capabilities("gpt-6-luna") == OPENAI_COMPAT_DEFAULT

    def test_replay_is_not_presumed_by_the_adapter(self) -> None:
        """Replay support is declared per lane, never assumed by the adapter.

        Assuming it would hand recorded reasoning to endpoints that reject it or
        silently ignore it.  The operator-owned default is fail-closed; a lane that
        can carry replay declares ``supports_reasoning_replay`` on its own row.
        """
        for surface in ("chat", "responses"):
            caps = create_provider(PROVIDER_NAME, api_surface=surface).get_capabilities(
                "qwen3.8-flash"
            )
            assert caps.supports_reasoning_replay is False
            assert caps.reasoning_effort_values == ()

    def test_both_surfaces_report_the_surface_they_ride(self) -> None:
        assert SwitchyardResponsesProvider._switchyard_surface == "responses"
        assert SwitchyardChatProvider._switchyard_surface == "chat"


class TestTheCrossingRunsOnEverySurface:
    """The crossing must run before the surface sees the messages.

    Each surface defines its own ``create_streaming`` and is expected to lower the
    ledger through ``_lower_messages`` first; the parent is the place that would
    receive an unresolved binding if a surface ever stopped doing that, so the parent
    is what these tests watch.
    """

    def _message(self) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": "the ledger text",
            "_provider_content": [copy.deepcopy(_ENCRYPTED), copy.deepcopy(_PLAINTEXT)],
        }

    def test_a_chat_surface_hands_its_parent_the_text_without_the_binding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chat carries readable text and has no field for a binding to survive in."""
        handed_to_surface: list[list[dict[str, Any]]] = []

        def _capture(self: object, **kwargs: Any) -> Any:
            handed_to_surface.append(kwargs["messages"])
            return iter(())

        monkeypatch.setattr(OpenAIChatCompletionsProvider, "create_streaming", _capture)

        provider = SwitchyardChatProvider()
        assert list(provider.create_streaming(messages=[self._message()])) == []

        assert len(handed_to_surface) == 1, "the surface never reached its parent"
        crossed = handed_to_surface[0][0]["_provider_content"]
        assert len(crossed) == 2
        assert "encrypted_content" not in crossed[0] and "id" not in crossed[0]
        assert crossed[0]["summary"][0]["text"] == "because"
        assert crossed[1] == _PLAINTEXT
        transfer = provider.last_reasoning_transfer
        assert (transfer.kept, transfer.stripped, transfer.dropped) == (2, 0, 0), (
            "the retained list and the reported crossing disagree"
        )
        assert not transfer.lossy

    def test_a_responses_surface_hands_its_parent_the_item_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lane's own item is that provider's object, and crosses with nothing removed."""
        handed_to_surface: list[list[dict[str, Any]]] = []

        def _capture(self: object, **kwargs: Any) -> Any:
            handed_to_surface.append(kwargs["messages"])
            return iter(())

        monkeypatch.setattr(OpenAIResponsesProvider, "create_streaming", _capture)

        provider = SwitchyardResponsesProvider()
        assert list(provider.create_streaming(messages=[self._message()])) == []

        assert len(handed_to_surface) == 1, "the surface never reached its parent"
        crossed = handed_to_surface[0][0]["_provider_content"]
        # The bound block keeps every field it arrived with; the untagged plain block
        # has no handle for the parent to build an item from, so it does not cross.
        assert crossed == [_ENCRYPTED]
        transfer = provider.last_reasoning_transfer
        assert (transfer.kept, transfer.stripped, transfer.dropped) == (1, 0, 1), (
            "the retained list and the reported crossing disagree"
        )
        assert transfer.losses == (LOSS_ITEM_UNREPRESENTABLE,)

    def test_a_surface_without_the_crossing_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control for the tests above: an uncrossed foreign binding must fail them."""
        handed_to_surface: list[list[dict[str, Any]]] = []

        def _capture(self: object, **kwargs: Any) -> Any:
            handed_to_surface.append(kwargs["messages"])
            return iter(())

        monkeypatch.setattr(OpenAIResponsesProvider, "create_streaming", _capture)
        monkeypatch.setattr(
            SwitchyardResponsesProvider,
            "_lower_messages",
            lambda self, messages: messages,  # the crossing, removed
        )
        message = {
            "role": "assistant",
            "content": "the ledger text",
            "_producer": "openai",
            "_provider_content": [copy.deepcopy(_ENCRYPTED)],
        }

        provider = SwitchyardResponsesProvider()
        list(provider.create_streaming(messages=[message]))

        crossed = handed_to_surface[0][0].get("_provider_content")
        assert crossed and crossed[0].get("encrypted_content") == "enc-blob", (
            "the removed crossing did not reproduce a bypass, so the tests above would be vacuous"
        )


class TestCrossProviderReasoningRule:
    def test_plain_text_crosses_unchanged(self) -> None:
        lowered, loss = lower_reasoning_block(_PLAINTEXT)
        assert lowered is _PLAINTEXT, "an unchanged block is handed on as it is"
        assert loss is None

    def test_content_shaped_plain_text_crosses_unchanged(self) -> None:
        assert lower_reasoning_block(_CONTENT_PLAINTEXT) == (_CONTENT_PLAINTEXT, None)

    def test_top_level_text_crosses_unchanged(self) -> None:
        """Switchyard's decoder reads a top-level ``text``, so this adapter must too."""
        block = {"type": "reasoning", "text": "because"}
        assert lower_reasoning_block(block) == (block, None)

    def test_an_encrypted_binding_is_removed_and_reported(self) -> None:
        lowered, loss = lower_reasoning_block(_ENCRYPTED)
        assert loss == LOSS_FOREIGN_ENCRYPTED
        assert lowered is not None
        assert "encrypted_content" not in lowered
        assert "id" not in lowered, "the item id is the handle that resolves the blob"
        assert lowered["summary"] == _ENCRYPTED["summary"], "the readable text survives"

    def test_an_encrypted_blob_without_text_does_not_cross(self) -> None:
        """The ciphertext is the reasoning, and only its issuer can resolve it."""
        assert lower_reasoning_block(
            {"type": "reasoning", "id": "rs_abc", "encrypted_content": "enc-blob"}
        ) == (None, LOSS_UNREPRESENTABLE)

    def test_a_signed_binding_is_removed_and_reported(self) -> None:
        lowered, loss = lower_reasoning_block(_SIGNED)
        assert loss == LOSS_NATIVE_SIGNED
        assert lowered == {"type": "thinking", "thinking": "because"}

    def test_a_block_bound_both_ways_keeps_no_binding(self) -> None:
        """Signature is checked first, but it is not the only thing removed."""
        block = {
            "type": "thinking",
            "id": "rs_abc",
            "thinking": "because",
            "signature": "sig-blob",
            "encrypted_content": "enc-blob",
        }
        lowered, loss = lower_reasoning_block(block)
        assert loss == LOSS_NATIVE_SIGNED
        assert lowered == {"type": "thinking", "thinking": "because"}

    def test_a_signature_without_text_does_not_cross(self) -> None:
        assert lower_reasoning_block({"type": "thinking", "signature": "sig-blob"}) == (
            None,
            LOSS_UNREPRESENTABLE,
        )

    def test_empty_reasoning_is_neither_kept_nor_a_loss(self) -> None:
        """No material is not the same as lost material."""
        assert lower_reasoning_block({"type": "reasoning"}) == (None, None)

    def test_lowering_never_edits_the_block_it_was_given(self) -> None:
        block = copy.deepcopy(_ENCRYPTED)
        lowered, _ = lower_reasoning_block(block)
        assert block == _ENCRYPTED
        assert lowered is not block

    def test_provenance_metadata_does_not_change_the_decision(self) -> None:
        """Which vendor recorded plain text is not what binds it."""
        recorded = {**_PLAINTEXT, "_producer": "anthropic"}
        assert lower_reasoning_block(recorded) == (recorded, None)


class TestBoundaryCrossing:
    """The retention pass, read against the carrier policy of the surface it serves.

    Every call states which surface it is lowering for: the same block is a crossing
    on one surface and a drop on the other, so a default would decide for the caller.
    """

    def _message(self) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": "the ledger text",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "get_weather", "arguments": "{}"}}
            ],
            "_provider_content": [copy.deepcopy(_ENCRYPTED), copy.deepcopy(_PLAINTEXT)],
            "_producer": "openai",
        }

    def test_a_chat_surface_keeps_the_readable_text_of_a_bound_block(self) -> None:
        messages, transfer = retain_transferable_reasoning([self._message()], **_AS_CHAT)
        blocks = messages[0]["_provider_content"]
        assert (transfer.kept, transfer.stripped, transfer.dropped) == (2, 0, 0)
        assert not transfer.lossy, "a binding this surface cannot carry is not a cost"
        assert "encrypted_content" not in blocks[0]
        assert "id" not in blocks[0], "the item id is the handle that resolves the blob"
        assert blocks[0]["summary"] == _ENCRYPTED["summary"]
        assert blocks[1] == _PLAINTEXT

    def test_a_responses_surface_keeps_the_item_and_drops_the_handleless_block(self) -> None:
        message = self._message()
        message.pop("_producer", None)  # this surface's own blocks, so the shape decides
        messages, transfer = retain_transferable_reasoning([message], **_AS_RESPONSES)
        # The bound block is this provider's own object and crosses untouched; the
        # untagged plain block has no handle, and no item is built without one.
        assert messages[0]["_provider_content"] == [_ENCRYPTED]
        assert (transfer.kept, transfer.stripped, transfer.dropped) == (1, 0, 1)
        assert transfer.losses == (LOSS_ITEM_UNREPRESENTABLE,)

    def test_no_block_content_is_invented_at_the_boundary(self) -> None:
        """Nothing is substituted for what was removed, and nothing gains a field."""
        message = self._message()
        inputs = message["_provider_content"]
        messages, _ = retain_transferable_reasoning([message], **_AS_CHAT)
        for block in messages[0]["_provider_content"]:
            assert any(
                all(item in original.items() for item in block.items()) for original in inputs
            ), f"a block appeared at the boundary that the ledger never contained: {block}"

    def test_ledger_truth_is_never_rewritten(self) -> None:
        """Canonical content and tool calls cross unchanged."""
        original = self._message()
        messages, _ = retain_transferable_reasoning([copy.deepcopy(original)], **_AS_CHAT)
        assert messages[0]["content"] == original["content"]
        assert messages[0]["tool_calls"] == original["tool_calls"]

    def test_the_caller_s_blocks_keep_their_bindings(self) -> None:
        original = self._message()
        before = copy.deepcopy(original)
        retain_transferable_reasoning([original], **_AS_CHAT)
        assert original == before
        assert original["_provider_content"][0]["encrypted_content"] == "enc-blob"

    def test_a_turn_with_nothing_readable_loses_only_the_private_block_list(self) -> None:
        message = self._message()
        message["_provider_content"] = [
            {"type": "reasoning", "id": "rs_abc", "encrypted_content": "enc-blob"}
        ]
        messages, transfer = retain_transferable_reasoning([message], **_AS_CHAT)
        assert "_provider_content" not in messages[0]
        assert messages[0]["content"] == "the ledger text"
        assert transfer.dropped == 1 and transfer.stripped == 0
        assert transfer.losses == (LOSS_UNREPRESENTABLE,)

    def test_blocks_the_adapter_does_not_interpret_pass_through_untouched(self) -> None:
        message = self._message()
        message["_provider_content"] = [copy.deepcopy(_TOOL_CALL)]
        messages, transfer = retain_transferable_reasoning([message], **_AS_CHAT)
        assert messages[0]["_provider_content"][0] is message["_provider_content"][0]
        assert transfer.kept == 0 and transfer.stripped == 0 and transfer.dropped == 0
        assert not transfer.lossy

    def test_a_textless_block_is_omitted_without_being_counted_as_a_loss(self) -> None:
        """Dropping a block that carried nothing is housekeeping, not a loss."""
        message = self._message()
        message["_provider_content"] = [{"type": "reasoning"}, copy.deepcopy(_PLAINTEXT)]
        messages, transfer = retain_transferable_reasoning([message], **_AS_CHAT)
        assert [block["type"] for block in messages[0]["_provider_content"]] == ["reasoning"]
        assert transfer.kept == 1 and transfer.stripped == 0 and transfer.dropped == 0
        assert not transfer.lossy

    def test_messages_without_reasoning_pass_through_untouched(self) -> None:
        plain = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_1", "content": "18C"},
        ]
        messages, transfer = retain_transferable_reasoning(plain, **_AS_CHAT)
        assert messages == plain
        assert transfer.kept == 0 and transfer.dropped == 0 and not transfer.lossy

    def test_each_distinct_loss_class_is_reported_once(self) -> None:
        """Two classes, in the order the messages were seen, each recorded once."""
        old_lineage = self._message()  # both blocks recorded by another producer
        migrated = self._message()
        migrated.pop("_producer", None)
        migrated["_provider_content"] = [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "because"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "because"}]},
        ]
        _, transfer = retain_transferable_reasoning([old_lineage, migrated], **_AS_RESPONSES)
        assert transfer.losses == (LOSS_FOREIGN_PRODUCER, LOSS_ITEM_UNREPRESENTABLE)
        assert (transfer.kept, transfer.stripped, transfer.dropped) == (0, 0, 4)


class TestWhatTheSurfaceWillActuallyEmit:
    """A crossing is only kept or stripped when the surface's wire carries it.

    Counting the lowering alone reported blocks as crossed that the parent could not
    build an item from.  These tests read the report against the parent's own
    projection: a Responses item needs a string ``id``, and a Responses parent reads
    native blocks only from its own producer.  On a wire that carries bindings, a
    native item passes through untouched, so the report's ``kept`` follows what that
    projection emits rather than what this adapter kept back.
    """

    def _one(self, block: dict[str, Any], *, producer: str | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "the ledger text",
            "_provider_content": [block],
        }
        if producer is not None:
            message["_producer"] = producer
        return message

    def test_a_responses_item_with_its_handle_still_crosses(self) -> None:
        _, transfer = retain_transferable_reasoning(
            [self._one(copy.deepcopy(_HANDLED))], **_AS_RESPONSES
        )
        assert transfer.kept == 1 and not transfer.lossy

    def test_a_native_bound_block_crosses_whole(self) -> None:
        """Its id and encrypted payload are the provider's own replay state."""
        block = copy.deepcopy(_ENCRYPTED)
        messages, transfer = retain_transferable_reasoning([self._one(block)], **_AS_RESPONSES)
        assert (transfer.kept, transfer.stripped, transfer.dropped) == (1, 0, 0)
        assert not transfer.lossy
        crossed = messages[0]["_provider_content"][0]
        assert crossed is block, "the item is handed on, not rebuilt"
        assert crossed["encrypted_content"] == _ENCRYPTED["encrypted_content"]
        assert crossed["id"] == _ENCRYPTED["id"]

    def test_the_item_a_live_lane_returns_reaches_the_parent_byte_for_byte(self) -> None:
        """The shape a real turn captures, handle and payload included, is untouched."""
        messages, transfer = retain_transferable_reasoning(
            [self._one(copy.deepcopy(_LIVE_ITEM))], **_AS_RESPONSES
        )
        assert transfer.kept == 1 and not transfer.lossy
        crossed = messages[0]["_provider_content"][0]
        assert json.dumps(crossed) == json.dumps(_LIVE_ITEM), (
            "a field the endpoint issued was rebuilt or removed on the way through"
        )
        for field in ("id", "content", "encrypted_content"):
            assert crossed[field] == _LIVE_ITEM[field]

    def test_a_foreign_item_leaves_no_binding_behind_in_any_form(self) -> None:
        """A dropped foreign block is not lowered either: nothing of it is re-emitted."""
        messages, transfer = retain_transferable_reasoning(
            [self._one(copy.deepcopy(_LIVE_ITEM), producer="openai")], **_AS_RESPONSES
        )
        assert "_provider_content" not in messages[0]
        assert (transfer.kept, transfer.stripped, transfer.dropped) == (0, 0, 1)
        assert transfer.losses == (LOSS_FOREIGN_PRODUCER,)

    def test_a_plain_block_without_a_handle_is_a_drop_too(self) -> None:
        """An untagged legacy shape has no id, and the parent builds no item without one."""
        _, transfer = retain_transferable_reasoning(
            [self._one(copy.deepcopy(_PLAINTEXT))], **_AS_RESPONSES
        )
        assert transfer.dropped == 1
        assert transfer.losses == (LOSS_ITEM_UNREPRESENTABLE,)

    def test_a_block_recorded_by_another_lane_is_a_drop_whatever_its_shape(self) -> None:
        """A block the parent will not read has not crossed, representable or not."""
        _, transfer = retain_transferable_reasoning(
            [self._one(copy.deepcopy(_HANDLED), producer="openai")], **_AS_RESPONSES
        )
        assert (transfer.kept, transfer.stripped, transfer.dropped) == (0, 0, 1)
        assert transfer.losses == (LOSS_FOREIGN_PRODUCER,)

    def test_the_producer_rule_is_read_before_the_block_s_shape(self) -> None:
        """A foreign message reports the reason that decided it, not the shape's cost."""
        _, transfer = retain_transferable_reasoning(
            [self._one(copy.deepcopy(_ENCRYPTED), producer="anthropic")], **_AS_RESPONSES
        )
        assert transfer.dropped == 1
        assert transfer.losses == (LOSS_FOREIGN_PRODUCER,)
        assert LOSS_ITEM_UNREPRESENTABLE not in transfer.losses

    def test_this_surface_s_own_producer_and_an_untagged_block_both_cross(self) -> None:
        for producer in (None, PROVIDER_NAME):
            _, transfer = retain_transferable_reasoning(
                [self._one(copy.deepcopy(_HANDLED), producer=producer)], **_AS_RESPONSES
            )
            assert transfer.kept == 1, f"producer={producer!r} was reported as not crossing"
            assert not transfer.lossy

    def test_a_foreign_producer_changes_nothing_where_the_parent_has_no_filter(self) -> None:
        """Chat never projects the private list, so it has no producer to compare."""
        _, transfer = retain_transferable_reasoning(
            [self._one(copy.deepcopy(_ENCRYPTED), producer="anthropic")], **_AS_CHAT
        )
        assert transfer.kept == 1 and not transfer.lossy

    @pytest.mark.parametrize(
        ("block", "producer"),
        (
            (copy.deepcopy(_HANDLED), None),
            (copy.deepcopy(_HANDLED), PROVIDER_NAME),
            (copy.deepcopy(_HANDLED), "openai"),
            (copy.deepcopy(_ENCRYPTED), None),
            (copy.deepcopy(_PLAINTEXT), None),
            (copy.deepcopy(_LIVE_ITEM), None),
            (copy.deepcopy(_LIVE_ITEM), PROVIDER_NAME),
            (copy.deepcopy(_LIVE_ITEM), "openai"),
            # A handle of the wrong type is not a handle: the parent reads a string.
            (
                {
                    "type": "reasoning",
                    "id": 123,
                    "summary": [{"type": "summary_text", "text": "why"}],
                },
                None,
            ),
        ),
    )
    def test_the_report_matches_what_the_parent_projects(
        self, block: dict[str, Any], producer: str | None
    ) -> None:
        """The report is read against the parent's projection, not a copy of it.

        This drives the parent's own item builder with the lowered messages, so a
        change to what that builder requires (the ``id``, the producer filter) fails
        this test instead of leaving a report that describes a crossing the wire did
        not perform.
        """
        messages, transfer = retain_transferable_reasoning(
            [self._one(copy.deepcopy(block), producer=producer)], **_AS_RESPONSES
        )
        _, items = OpenAIResponsesProvider._convert_messages(
            messages,
            replay_reasoning_to_model=True,
            native_producer=SwitchyardResponsesProvider().provider_name,
        )
        emitted = any(item.get("type") == "reasoning" for item in items)
        assert emitted == (transfer.kept == 1), (
            "the crossing report and the parent's projection disagree"
        )
        assert emitted == (not transfer.lossy)

    def test_every_declared_surface_has_a_carrier_policy(self) -> None:
        """An unlisted surface fails closed at the crossing rather than silently."""
        for provider_class in (SwitchyardChatProvider, SwitchyardResponsesProvider):
            assert provider_class._switchyard_surface in _SURFACE_CARRIERS

    @pytest.mark.parametrize("surface", (None, "chat", "responses"))
    @pytest.mark.parametrize("producer", (None, PROVIDER_NAME, "openai"))
    def test_the_declared_surface_policy_decides_the_report(
        self, surface: str | None, producer: str | None
    ) -> None:
        """The module's own policy rows, driven through the providers a lane rides.

        The tests above state a policy at the call site; this one reads whatever the
        surface declares, so a row that stops matching what the surface does fails
        here rather than only in a live turn.
        """
        provider = create_provider(
            PROVIDER_NAME, **({} if surface is None else {"api_surface": surface})
        )
        provider._lower_messages([self._one(copy.deepcopy(_HANDLED), producer=producer)])
        transfer = provider.last_reasoning_transfer
        own = producer in (None, PROVIDER_NAME)
        if surface == "responses":
            assert (transfer.kept, transfer.dropped) == ((1, 0) if own else (0, 1))
            assert transfer.losses == (() if own else (LOSS_FOREIGN_PRODUCER,))
        else:
            # Neither chat row selects native blocks by producer.
            assert (transfer.kept, transfer.dropped) == (1, 0)
            assert transfer.losses == ()


class TestCrossingStateBelongsToTheTurn:
    """The adapter is shared between sessions; what a crossing records is not.

    The factory returns one adapter per surface, so a crossing outcome stored on
    the instance would outlive the turn that produced it: a second session
    crossing before the first has read its own counts would overwrite them, and
    the first session's ledger row would be billed against a turn that was not
    its own.  The outcome is therefore held per calling thread.

    The interleaving in the tests below is forced with events, never with sleeps:
    thread A crosses first and then waits, thread B crosses second and releases A,
    and only then does each read.  A per-instance attribute loses A's counts in
    every run, so this is a regression pin rather than a race that might pass.
    """

    _KEPT = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "a"}]}]
    _DROPPED = [{"type": "reasoning", "id": "rs_x", "encrypted_content": "blob"}]

    def _messages(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": "assistant", "content": "text", "_provider_content": blocks}]

    def test_the_crossing_state_of_one_session_does_not_reach_another(self) -> None:
        provider = create_provider(PROVIDER_NAME)
        assert create_provider(PROVIDER_NAME) is provider, (
            "this test pins a hazard that only exists because the adapter is shared"
        )

        a_crossed = threading.Event()
        b_crossed = threading.Event()
        seen: dict[str, ReasoningTransfer] = {}
        failures: list[BaseException] = []

        def cross_a() -> None:
            try:
                provider._lower_messages(self._messages(copy.deepcopy(self._KEPT)))
                a_crossed.set()
                b_crossed.wait(timeout=30)
                seen["a"] = provider.last_reasoning_transfer
            except BaseException as failure:  # noqa: BLE001 - re-raised in the test body
                failures.append(failure)

        def cross_b() -> None:
            try:
                a_crossed.wait(timeout=30)
                provider._lower_messages(self._messages(copy.deepcopy(self._DROPPED)))
                b_crossed.set()
                seen["b"] = provider.last_reasoning_transfer
            except BaseException as failure:  # noqa: BLE001 - re-raised in the test body
                failures.append(failure)

        threads = [
            threading.Thread(target=cross_a, name="crossing-a", daemon=True),
            threading.Thread(target=cross_b, name="crossing-b", daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert failures == [], f"a crossing thread raised: {failures}"
        assert not any(thread.is_alive() for thread in threads), "the interleaving deadlocked"
        assert set(seen) == {"a", "b"}, "a crossing thread never reached its read"

        # Two payloads with no overlapping counts: only a per-thread outcome can
        # satisfy both readers, and a shared attribute fails A on every run.
        assert (seen["a"].kept, seen["a"].dropped) == (1, 0)
        assert seen["a"].losses == ()
        assert (seen["b"].kept, seen["b"].dropped) == (0, 1)
        assert seen["b"].losses == (LOSS_UNREPRESENTABLE,)

    def test_a_surface_that_crossed_nothing_reports_an_empty_transfer(self) -> None:
        """A read before any crossing is an empty outcome, not a missing one."""
        for provider in (SwitchyardChatProvider(), SwitchyardResponsesProvider()):
            transfer = provider.last_reasoning_transfer
            assert transfer == ReasoningTransfer()
            assert transfer.kept == 0 and transfer.stripped == 0 and transfer.dropped == 0
            assert not transfer.lossy
