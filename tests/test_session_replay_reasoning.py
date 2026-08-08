"""Tests for session-level ``replay_reasoning_to_model`` plumbing.

Phase 2 of optional reasoning persistence reads the per-model
``ModelConfig.replay_reasoning_to_model`` flag at the wire-build call
site and threads it through ``provider.create_streaming`` (the one
transport post-#831).  These tests pin:

1. The resolver (``model_turn.resolve_replay_reasoning_to_model``) walks
   the registry correctly and falls back to ``False`` (the conservative
   default matching the migration server_default) when the lookup fails.
2. The streaming wire-build call site actually passes the resolved flag
   down — post-#832 that call site is ``model_turn``, reached through
   ``session._stream_response`` and its lane-swap fallback walk.  Without
   this the Phase 2 work is dead code (the strip-when-False predicate
   never fires).
3. The non-streaming call site at ``session.py:_utility_completion``
   does the same, through the same ``model_turn`` seam.

Drives the real resolver against a stub registry, then reads the kwarg a
fake provider captured — the same assertion surface as before the fold,
one caller down.  The capability half of the resolver's AND-gate now
reaches it as the LANE's capabilities (provider static table + the
registry's operator overrides) instead of a ``capabilities=`` argument at
the call site, so tests that supplied their own ``ModelCapabilities``
state it through the stub registry's ``capabilities`` dict.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from tests._parity_832 import SCENARIOS
from tests._session_helpers import (
    FakeAnthropicBlock,
    as_stream,
    fake_anthropic_stream,
    mock_completion_result,
    scripted_provider,
)
from tests._session_helpers import make_session as _make_session
from turnstone.core.model_turn import resolve_lane, resolve_replay_reasoning_to_model
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.trajectory import Turn, turns_from_dicts


def _registry_with_flag(
    persist: bool = True,
    replay: bool = False,
    caps_overrides: dict[str, Any] | None = None,
) -> Any:
    """Stub registry returning a ModelConfig-shaped object with the
    flags under test.  *caps_overrides* rides the config's
    ``capabilities`` dict, which is how an operator states a model
    capability the lane must resolve."""
    return SimpleNamespace(
        get_config=lambda alias: SimpleNamespace(
            surface_persisted_reasoning=persist,
            replay_reasoning_to_model=replay,
            capabilities=dict(caps_overrides or {}),
        )
    )


def _flag_capture_provider(*, supports_replay: bool = True) -> MagicMock:
    """Provider fake recording the kwargs ``model_turn`` built, then
    replaying a finished stream.

    The eager ``cancel_ref`` arming is mandatory at this seam (the
    creation-vs-midstream classifier reads it), so the shape comes from
    ``tests._parity_832.scripted_provider`` verbatim; only the advertised
    reasoning-replay capability is per-test.
    """
    provider = scripted_provider(SCENARIOS["content_only"])
    provider.get_capabilities.return_value = ModelCapabilities(
        supports_reasoning_replay=supports_replay
    )
    return provider


def _bind_session_lane(
    session: Any,
    *,
    registry: Any,
    provider: Any,
    alias: str,
    model: str | None = None,
    client: Any | None = None,
    capabilities: ModelCapabilities | None = None,
) -> None:
    """Install a complete provider/client/model/capability lane for a test."""
    session._registry = registry
    current_lane = session._model_binding.lane
    lane = resolve_lane(
        provider,
        current_lane.client if client is None else client,
        current_lane.model if model is None else model,
        alias=alias,
        registry=registry,
        capabilities=capabilities,
    )
    session._model_binding = replace(session._model_binding, lane=lane)


def _drive_stream(
    session: Any,
    provider: MagicMock,
    *,
    registry: Any,
    alias: str,
) -> dict[str, Any]:
    """Run ONE real streaming turn against *provider* and return the
    kwargs that reached ``create_streaming``."""
    _bind_session_lane(session, registry=registry, provider=provider, alias=alias)
    session.messages.append(Turn.user("hi"))
    session._stream_response(0)
    kwargs: dict[str, Any] = provider.create_streaming.call_args.kwargs
    return kwargs


class TestResolveReplayReasoningToModel:
    """Direct unit tests for the resolver.

    The session wrapper this class used to call was deleted with the
    ``_try_stream`` seam (#832): the lane carries the registry and the
    alias, and ``model_turn`` reads the flag off them per call.  Same
    resolver, one indirection down — so the case table is unchanged.
    """

    def test_returns_false_when_no_registry(self) -> None:
        assert resolve_replay_reasoning_to_model(None, "anything") is False

    def test_returns_false_when_no_alias(self) -> None:
        # A lane outside the registry carries ``alias=""``.
        assert resolve_replay_reasoning_to_model(_registry_with_flag(replay=True), "") is False

    def test_returns_false_default(self) -> None:
        registry = _registry_with_flag(replay=False)
        assert resolve_replay_reasoning_to_model(registry, "claude-opus-4-7") is False

    def test_returns_true_when_flag_set(self) -> None:
        registry = _registry_with_flag(replay=True)
        assert resolve_replay_reasoning_to_model(registry, "claude-opus-4-7") is True

    def test_alias_selects_its_own_config(self) -> None:
        # The flag tracks the alias the LANE resolved, not any session
        # default — the property the fallback walk depends on (pinned
        # end-to-end by test_fallback_alias_uses_its_own_flag below).
        def per_alias(alias: str) -> Any:
            return SimpleNamespace(
                replay_reasoning_to_model=(alias == "needs-replay"),
            )

        registry = SimpleNamespace(get_config=per_alias)
        assert resolve_replay_reasoning_to_model(registry, "primary") is False
        assert resolve_replay_reasoning_to_model(registry, "needs-replay") is True

    def test_returns_false_on_registry_exception(self) -> None:
        def boom(alias: str) -> Any:
            raise KeyError(alias)

        registry = SimpleNamespace(get_config=boom)
        # Conservative fallback — losing the strip is a UX nuisance,
        # but accepting wire-side reasoning replay against an unknown
        # operator preference is a worse default.
        assert resolve_replay_reasoning_to_model(registry, "missing") is False

    def test_caps_none_preserves_back_compat(self) -> None:
        # When ``caps`` is omitted, the resolver returns the operator
        # flag unchanged — matching pre-PR behaviour for any caller
        # that hasn't been updated to thread caps yet.
        registry = _registry_with_flag(replay=True)
        assert resolve_replay_reasoning_to_model(registry, "claude-opus-4-7") is True
        assert resolve_replay_reasoning_to_model(registry, "claude-opus-4-7", caps=None) is True

    def test_caps_supports_replay_true_passes_through(self) -> None:
        registry = _registry_with_flag(replay=True)
        caps = ModelCapabilities(supports_reasoning_replay=True)
        assert resolve_replay_reasoning_to_model(registry, "claude-opus-4-7", caps=caps) is True

    def test_caps_supports_replay_false_blocks_replay(self) -> None:
        # Operator flipped replay=True but the model's capability
        # advertises supports_reasoning_replay=False — AND-gate blocks
        # replay so the strip predicate runs at the wire build.
        registry = _registry_with_flag(replay=True)
        caps = ModelCapabilities(supports_reasoning_replay=False)
        alias = "hypothetical-no-replay-claude"
        assert resolve_replay_reasoning_to_model(registry, alias, caps=caps) is False

    def test_caps_supports_replay_true_does_not_force_replay(self) -> None:
        # Capability True but operator flag False — result must be
        # False (the AND has to be False on either side).
        registry = _registry_with_flag(replay=False)
        caps = ModelCapabilities(supports_reasoning_replay=True)
        assert resolve_replay_reasoning_to_model(registry, "claude-opus-4-7", caps=caps) is False


class TestStreamingCallSitePassesFlag:
    """Pin that the streaming turn actually passes the resolved flag to
    ``provider.create_streaming`` — without this the Phase 2 work is
    dead code at the call site.

    The call site is ``model_turn`` now, driven through the real
    ``_stream_response`` wrapper (creation walk → drain → finalize), so
    the flag rides the lane the walk actually served the turn on.
    """

    def test_replay_true_propagates_to_provider(self) -> None:
        session = _make_session()
        registry = _registry_with_flag(replay=True)
        kwargs = _drive_stream(
            session,
            _flag_capture_provider(supports_replay=True),
            registry=registry,
            alias="claude-opus-4-7",
        )
        assert kwargs["replay_reasoning_to_model"] is True

    def test_replay_false_propagates_to_provider(self) -> None:
        session = _make_session()
        registry = _registry_with_flag(replay=False)
        # Capability advertises replay support: the False comes from the
        # operator flag alone, not from the AND-gate's other half.
        kwargs = _drive_stream(
            session,
            _flag_capture_provider(supports_replay=True),
            registry=registry,
            alias="claude-opus-4-7",
        )
        assert kwargs["replay_reasoning_to_model"] is False

    def test_fallback_alias_uses_its_own_flag(self) -> None:
        # When the primary fails and we fall back to an alias with a
        # different flag, the flag MUST track the resolved alias —
        # not the session's primary alias.
        session = _make_session()

        def per_alias(alias: str) -> Any:
            return SimpleNamespace(
                replay_reasoning_to_model=(alias == "fallback-with-replay"),
                capabilities={},
            )

        fb_provider = _flag_capture_provider(supports_replay=True)
        registry = SimpleNamespace(
            get_config=per_alias,
            fallback=["fallback-with-replay"],
            resolve_binding=lambda alias: (MagicMock(), "fallback-model", None, fb_provider, None),
        )
        # The primary lane dies at CREATION (raises without arming its
        # cancel_ref), which is what sends the walk to the next alias; a
        # non-retryable class keeps the ladder from burning backoff.
        primary = _flag_capture_provider(supports_replay=True)
        primary.create_streaming = MagicMock(side_effect=RuntimeError("primary is down"))
        _drive_stream(session, primary, registry=registry, alias="primary")
        # Resolved against the FALLBACK alias, not the session's primary.
        assert fb_provider.create_streaming.call_args.kwargs["replay_reasoning_to_model"] is True
        # ...and the primary's own attempt resolved its own alias' flag.
        assert primary.create_streaming.call_args.kwargs["replay_reasoning_to_model"] is False


class TestSessionToWireBoundaryIntegration:
    """End-to-end integration: session._stream_response -> model_turn ->
    real AnthropicProvider.create_streaming -> captured Anthropic SDK
    boundary call.  Verifies the strip-when-False predicate actually
    fires at the wire payload, not just at the captured kwarg.

    The provider-fake tests above (TestStreamingCallSitePassesFlag) pin
    that the streaming turn PASSES the flag; this test pins that the real
    provider USES it.  Together they catch:
      - kwarg renamed at provider boundary -> fake-provider tests still
        pass, this one fails on its real-provider assertion.
      - _convert_messages stops reading the kwarg -> fake-provider tests
        still pass, this one fails because the wire payload still carries
        the thinking block.
      - the streaming turn stops calling create_streaming -> fake-provider
        tests fail on the captured kwarg, this one fails because the SDK
        boundary was never reached.

    Drives through the real ``AnthropicProvider`` with a mock client
    whose ``client.messages.stream`` is captured — the smallest possible
    surface that crosses the session->provider->wire boundary chain.

    Negative-tested: temporarily reverting
    ``_anthropic.py:create_streaming``'s
    ``self._convert_messages(messages, replay_reasoning_to_model=...)``
    call to drop the kwarg makes the wire payload carry the thinking
    block again; ``test_replay_false_strips_thinking_at_wire`` then
    fails with ``Strip predicate did not fire at wire boundary``.
    Restoring the kwarg makes it pass — confirming the test gates the
    actual wire-build invariant rather than the captured kwarg.
    """

    def _stub_anthropic_client(self) -> tuple[MagicMock, dict[str, object]]:
        """Build a mock Anthropic client + captured-kwargs dict.

        ``client.messages.stream(**kwargs)`` returns the real event
        grammar for a one-block reply, so the fused create+drain reaches
        a finish reason instead of exhausting finish-less (which the
        post-#832 seam re-issues as an ``IncompleteStreamError``).
        """
        captured: dict[str, object] = {}

        def stream(**kwargs: object) -> object:
            captured.update(kwargs)
            return fake_anthropic_stream([FakeAnthropicBlock(type="text", text="ok")])

        client = MagicMock()
        client.messages.stream = stream
        return client, captured

    def _drive_session_through_anthropic(
        self,
        replay_flag: bool,
        msgs: list[dict[str, object]],
        caps_overrides: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        """Run one real streaming turn against a real AnthropicProvider
        with the resolver pre-set to *replay_flag*.  Returns the kwargs
        dict that reached the (mocked) Anthropic SDK boundary.

        *msgs* seeds the session trajectory (the wire list is built from
        it inside ``model_turn`` now — lowering plus the session's own
        ``prepare_wire`` passes — instead of being handed to the seam).
        """
        from turnstone.core.providers._anthropic import AnthropicProvider

        session = _make_session()
        registry = _registry_with_flag(replay=replay_flag, caps_overrides=caps_overrides)
        client, captured = self._stub_anthropic_client()
        _bind_session_lane(
            session,
            registry=registry,
            provider=AnthropicProvider(),
            client=client,
            model="claude-opus-4-7",
            alias="claude-opus-4-7",
        )
        session.messages = turns_from_dicts(msgs)
        session._stream_response(0)
        return captured

    def test_replay_false_strips_thinking_at_wire(self) -> None:
        msgs: list[dict[str, Any]] = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "Final answer.",
                "_provider_content": [
                    {"type": "thinking", "thinking": "secret reasoning", "signature": "s"},
                    {"type": "text", "text": "Final answer."},
                ],
            },
            {"role": "user", "content": "ack"},
        ]
        captured = self._drive_session_through_anthropic(False, msgs)
        # Anthropic SDK was called.
        wire_msgs = captured.get("messages")
        assert isinstance(wire_msgs, list), (
            f"Expected messages= list at SDK boundary, got {captured}"
        )
        # Walk the wire payload — the thinking block must NOT be present
        # in the assistant turn's content blocks.
        assistant = next(m for m in wire_msgs if m["role"] == "assistant")
        block_types = [b.get("type") for b in assistant["content"] if isinstance(b, dict)]
        assert "thinking" not in block_types, (
            f"Strip predicate did not fire at wire boundary: blocks={block_types}"
        )
        # Defense-in-depth: the secret reasoning text must not appear
        # anywhere in the wire payload.
        flat = repr(captured)
        assert "secret reasoning" not in flat, "Reasoning text leaked into the SDK boundary payload"

    def test_replay_true_preserves_thinking_at_wire(self) -> None:
        msgs: list[dict[str, Any]] = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "Final answer.",
                "_provider_content": [
                    {"type": "thinking", "thinking": "kept reasoning", "signature": "s"},
                    {"type": "text", "text": "Final answer."},
                ],
            },
            {"role": "user", "content": "ack"},
        ]
        captured = self._drive_session_through_anthropic(True, msgs)
        wire_msgs = captured.get("messages")
        assert isinstance(wire_msgs, list)
        assistant = next(m for m in wire_msgs if m["role"] == "assistant")
        block_types = [b.get("type") for b in assistant["content"] if isinstance(b, dict)]
        assert "thinking" in block_types, (
            f"Replay-true did not preserve thinking at wire: blocks={block_types}"
        )

    def test_capability_false_strips_thinking_even_when_operator_flag_true(self) -> None:
        # Mirror of the OpenAI Responses ``test_capability_false_omits_
        # include_even_when_flag_true`` test below: operator flips
        # replay=True but the model's capability advertises
        # supports_reasoning_replay=False.  AND-gate at the resolver
        # blocks replay, so the strip predicate fires at the wire and
        # the thinking block does NOT reach the SDK boundary.  The
        # capability reaches the resolver as the LANE's — an operator
        # override on the alias, since the lane resolves its own caps
        # rather than taking them from the caller.
        msgs: list[dict[str, Any]] = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "Final answer.",
                "_provider_content": [
                    {"type": "thinking", "thinking": "secret reasoning", "signature": "s"},
                    {"type": "text", "text": "Final answer."},
                ],
            },
            {"role": "user", "content": "ack"},
        ]

        captured = self._drive_session_through_anthropic(
            True,  # operator opted in
            msgs,
            caps_overrides={"supports_reasoning_replay": False},
        )

        wire_msgs = captured.get("messages")
        assert isinstance(wire_msgs, list), (
            f"Expected messages= list at SDK boundary, got {captured}"
        )
        assistant = next(m for m in wire_msgs if m["role"] == "assistant")
        block_types = [b.get("type") for b in assistant["content"] if isinstance(b, dict)]
        assert "thinking" not in block_types, (
            "Capability gate did not block replay: thinking block reached the wire "
            f"despite supports_reasoning_replay=False (blocks={block_types})"
        )
        flat = repr(captured)
        assert "secret reasoning" not in flat, (
            "Reasoning text leaked into the SDK boundary payload despite capability gate"
        )


class TestSessionToOpenAIResponsesBoundaryIntegration:
    """End-to-end integration: session._stream_response -> model_turn ->
    real OpenAIResponsesProvider.create_streaming -> captured Responses
    SDK boundary call.  Mirrors the AnthropicProvider test above
    but for the path-2 (Responses API) replay flow.

    Pins the include= request kwarg + reasoning input-item emission
    actually fire at the wire boundary when the operator flag and
    model capability both allow.
    """

    def _stub_responses_client(self) -> tuple[MagicMock, dict[str, object]]:
        """Mock OpenAI Responses client.  ``client.responses.create``
        captures kwargs and returns a stream carrying only the terminal
        event — the fused create+drain needs a finish reason (a
        finish-less exhaust is an ``IncompleteStreamError`` post-#832)."""
        captured: dict[str, object] = {}

        def create(**kwargs: object) -> object:
            captured.update(kwargs)
            return iter([SimpleNamespace(type="response.completed", response=None)])

        client = MagicMock()
        client.responses.create = create
        return client, captured

    def _registry_with_reasoning_capability(
        self, replay: bool = True, supports_replay: bool = True
    ) -> Any:
        """Stub registry stating both halves of the gate: the operator
        flag and — as an alias capability override, the operator's way to
        state one — the model's reasoning-replay support.  The lane
        resolves its own capabilities now, so this is where a test says
        what the model can do."""
        return SimpleNamespace(
            get_config=lambda alias: SimpleNamespace(
                replay_reasoning_to_model=replay,
                capabilities={"supports_reasoning_replay": supports_replay},
            ),
        )

    def _drive(
        self,
        session: Any,
        msgs: list[dict[str, Any]],
        *,
        registry: Any,
        alias: str,
    ) -> dict[str, object]:
        """One real streaming turn through the real Responses provider."""
        from turnstone.core.providers._openai_responses import OpenAIResponsesProvider

        client, captured = self._stub_responses_client()
        _bind_session_lane(
            session,
            registry=registry,
            provider=OpenAIResponsesProvider(),
            client=client,
            model=alias,
            alias=alias,
        )
        session.messages = turns_from_dicts(msgs)
        session._stream_response(0)
        return captured

    def test_replay_true_adds_include_to_responses_request(self) -> None:
        session = _make_session()
        registry = self._registry_with_reasoning_capability(replay=True, supports_replay=True)
        captured = self._drive(
            session,
            [{"role": "user", "content": "hi"}],
            registry=registry,
            alias="gpt-5",
        )
        assert captured.get("include") == ["reasoning.encrypted_content"]

    def test_replay_false_omits_include(self) -> None:
        session = _make_session()
        registry = self._registry_with_reasoning_capability(replay=False, supports_replay=True)
        captured = self._drive(
            session,
            [{"role": "user", "content": "hi"}],
            registry=registry,
            alias="gpt-5",
        )
        assert "include" not in captured

    def test_capability_false_omits_include_even_when_flag_true(self) -> None:
        # Operator flips replay=True but the model has
        # supports_reasoning_replay=False (e.g. gpt-4o via Responses).
        # Capability gate prevents the include= from being sent.
        session = _make_session()
        registry = self._registry_with_reasoning_capability(replay=True, supports_replay=False)
        captured = self._drive(
            session,
            [{"role": "user", "content": "hi"}],
            registry=registry,
            alias="gpt-4o",
        )
        assert "include" not in captured

    def test_replay_true_emits_reasoning_input_item(self) -> None:
        session = _make_session()
        registry = self._registry_with_reasoning_capability(replay=True, supports_replay=True)
        # Multi-turn conversation with stored reasoning on assistant turn.
        msgs: list[dict[str, Any]] = [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": "Final answer.",
                "_provider_content": [
                    {
                        "type": "reasoning",
                        "id": "r_xyz",
                        "summary": [{"type": "summary_text", "text": "I thought"}],
                        "encrypted_content": "blob",
                    }
                ],
            },
            {"role": "user", "content": "follow-up"},
        ]
        captured = self._drive(session, msgs, registry=registry, alias="gpt-5")
        # Walk the wire input items — one of them must be the reasoning
        # round-trip (id matches what we stored).
        wire_input = captured.get("input")
        assert isinstance(wire_input, list)
        reasoning_items = [it for it in wire_input if it.get("type") == "reasoning"]
        assert len(reasoning_items) == 1
        assert reasoning_items[0]["id"] == "r_xyz"
        assert reasoning_items[0]["encrypted_content"] == "blob"


class TestUtilityCompletionPassesFlag:
    """Non-streaming utility path (title gen, compaction, extraction) —
    same plumbing requirement as streaming."""

    def test_utility_completion_passes_resolved_flag(self) -> None:
        session = _make_session()
        registry = _registry_with_flag(replay=True)
        captured: dict[str, Any] = {}

        def capture_streaming(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return as_stream(mock_completion_result("title"))

        mock_provider = MagicMock()
        mock_provider.create_streaming = capture_streaming
        caps = ModelCapabilities(max_output_tokens=0, supports_reasoning_replay=True)
        _bind_session_lane(
            session,
            registry=registry,
            provider=mock_provider,
            alias="claude-opus-4-7",
            model="claude-opus-4-7",
            capabilities=caps,
        )
        session._utility_completion(
            [Turn.user("summarize")],
            max_tokens=512,
            temperature=0.3,
        )
        assert captured["replay_reasoning_to_model"] is True
