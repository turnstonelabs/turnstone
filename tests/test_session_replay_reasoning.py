"""Tests for session-level ``replay_reasoning_to_model`` plumbing.

Phase 2 of optional reasoning persistence reads the per-model
``ModelConfig.replay_reasoning_to_model`` flag at the wire-build call
site and threads it through ``provider.create_streaming`` /
``provider.create_completion``.  These tests pin:

1. The resolver helper (``ChatSession._resolve_replay_reasoning_to_model``)
   walks the registry correctly and falls back to ``False`` (the
   conservative default matching the migration server_default) when
   the lookup fails.
2. The streaming wire-build call site at ``session.py:_try_stream``
   actually passes the resolved flag down — without this, the Phase
   2 work is dead code (the strip-when-False predicate never fires).
3. The non-streaming wire-build call site at
   ``session.py:_utility_completion`` does the same.

Drives through the real ``ChatSession._resolve_replay_reasoning_to_model``
with a stub registry, then captures the kwarg passed to a mock provider
to verify the flow end-to-end.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from turnstone.core.session import ChatSession
from turnstone.core.session_ui_base import SessionUIBase


class _NullUI(SessionUIBase):
    """Bare-bones UI satisfying the SessionUIBase contract for these tests."""

    def __init__(self) -> None:
        super().__init__()


def _make_session(**kwargs: Any) -> ChatSession:
    defaults: dict[str, Any] = {
        "client": MagicMock(),
        "model": "test-model",
        "ui": _NullUI(),
        "instructions": None,
        "temperature": 0.5,
        "max_tokens": 4096,
        "tool_timeout": 30,
    }
    defaults.update(kwargs)
    return ChatSession(**defaults)


def _registry_with_flag(persist: bool = True, replay: bool = False) -> Any:
    """Stub registry returning a ModelConfig-shaped object with the
    flags under test."""
    return SimpleNamespace(
        get_config=lambda alias: SimpleNamespace(
            persist_reasoning=persist,
            replay_reasoning_to_model=replay,
        )
    )


class TestResolveReplayReasoningToModel:
    """Direct unit tests for the resolver."""

    def test_returns_false_when_no_registry(self) -> None:
        session = _make_session()
        session._registry = None
        session._model_alias = "anything"
        assert session._resolve_replay_reasoning_to_model() is False

    def test_returns_false_when_no_alias(self) -> None:
        session = _make_session()
        session._registry = _registry_with_flag(replay=True)
        session._model_alias = ""
        assert session._resolve_replay_reasoning_to_model() is False

    def test_returns_false_default(self) -> None:
        session = _make_session()
        session._registry = _registry_with_flag(replay=False)
        session._model_alias = "claude-opus-4-7"
        assert session._resolve_replay_reasoning_to_model() is False

    def test_returns_true_when_flag_set(self) -> None:
        session = _make_session()
        session._registry = _registry_with_flag(replay=True)
        session._model_alias = "claude-opus-4-7"
        assert session._resolve_replay_reasoning_to_model() is True

    def test_explicit_alias_arg_overrides_default(self) -> None:
        session = _make_session()

        def per_alias(alias: str) -> Any:
            return SimpleNamespace(
                replay_reasoning_to_model=(alias == "needs-replay"),
            )

        session._registry = SimpleNamespace(get_config=per_alias)
        session._model_alias = "primary"
        # Default reads session._model_alias → False.
        assert session._resolve_replay_reasoning_to_model() is False
        # Explicit alias arg → True for "needs-replay".
        assert session._resolve_replay_reasoning_to_model("needs-replay") is True

    def test_returns_false_on_registry_exception(self) -> None:
        session = _make_session()

        def boom(alias: str) -> Any:
            raise KeyError(alias)

        session._registry = SimpleNamespace(get_config=boom)
        session._model_alias = "missing"
        # Conservative fallback — losing the strip is a UX nuisance,
        # but accepting wire-side reasoning replay against an unknown
        # operator preference is a worse default.
        assert session._resolve_replay_reasoning_to_model() is False


class TestStreamingCallSitePassesFlag:
    """Pin that ``_try_stream`` actually passes the resolved flag to
    ``provider.create_streaming`` — without this the Phase 2 work is
    dead code at the call site."""

    def test_replay_true_propagates_to_provider(self) -> None:
        session = _make_session()
        session._registry = _registry_with_flag(replay=True)
        session._model_alias = "claude-opus-4-7"
        # Stub provider: capture the kwargs passed to create_streaming.
        captured: dict[str, Any] = {}

        def capture_streaming(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return iter([])

        mock_provider = MagicMock()
        mock_provider.create_streaming = capture_streaming
        with (
            patch.object(session, "_get_active_tools", return_value=None),
            patch.object(session, "_provider_extra_params", return_value=None),
            patch.object(session, "_get_deferred_names", return_value=frozenset()),
            patch.object(session, "_check_cancelled"),
        ):
            session._try_stream(
                client=MagicMock(),
                model="claude-opus-4-7",
                msgs=[{"role": "user", "content": "hi"}],
                provider=mock_provider,
                model_alias="claude-opus-4-7",
            )
        assert captured["replay_reasoning_to_model"] is True

    def test_replay_false_propagates_to_provider(self) -> None:
        session = _make_session()
        session._registry = _registry_with_flag(replay=False)
        session._model_alias = "claude-opus-4-7"
        captured: dict[str, Any] = {}

        def capture_streaming(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return iter([])

        mock_provider = MagicMock()
        mock_provider.create_streaming = capture_streaming
        with (
            patch.object(session, "_get_active_tools", return_value=None),
            patch.object(session, "_provider_extra_params", return_value=None),
            patch.object(session, "_get_deferred_names", return_value=frozenset()),
            patch.object(session, "_check_cancelled"),
        ):
            session._try_stream(
                client=MagicMock(),
                model="claude-opus-4-7",
                msgs=[{"role": "user", "content": "hi"}],
                provider=mock_provider,
                model_alias="claude-opus-4-7",
            )
        assert captured["replay_reasoning_to_model"] is False

    def test_fallback_alias_uses_its_own_flag(self) -> None:
        # When the primary fails and we fall back to an alias with a
        # different flag, the flag MUST track the resolved alias —
        # not the session's primary alias.
        session = _make_session()

        def per_alias(alias: str) -> Any:
            return SimpleNamespace(
                replay_reasoning_to_model=(alias == "fallback-with-replay"),
            )

        session._registry = SimpleNamespace(get_config=per_alias)
        session._model_alias = "primary"  # primary has replay=False
        captured: dict[str, Any] = {}

        def capture_streaming(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return iter([])

        mock_provider = MagicMock()
        mock_provider.create_streaming = capture_streaming
        with (
            patch.object(session, "_get_active_tools", return_value=None),
            patch.object(session, "_provider_extra_params", return_value=None),
            patch.object(session, "_get_deferred_names", return_value=frozenset()),
            patch.object(session, "_check_cancelled"),
        ):
            session._try_stream(
                client=MagicMock(),
                model="fallback-model",
                msgs=[{"role": "user", "content": "hi"}],
                provider=mock_provider,
                model_alias="fallback-with-replay",
            )
        # Resolved against the FALLBACK alias, not the session's primary.
        assert captured["replay_reasoning_to_model"] is True


class TestSessionToWireBoundaryIntegration:
    """End-to-end integration: session._try_stream -> real
    AnthropicProvider.create_streaming -> captured Anthropic SDK
    boundary call.  Verifies the strip-when-False predicate actually
    fires at the wire payload, not just at the captured kwarg.

    The bare-function-stub tests above (TestStreamingCallSitePassesFlag)
    pin that ``_try_stream`` PASSES the flag; this test pins that the
    real provider USES it.  Together they catch:
      - kwarg renamed at provider boundary -> stub-tests still pass,
        this one fails on its real-provider assertion.
      - _convert_messages stops reading the kwarg -> stub-tests still
        pass, this one fails because the wire payload still carries
        the thinking block.
      - _try_stream stops calling create_streaming -> stub-tests fail
        on the captured kwarg, this one fails because the SDK boundary
        was never reached.

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

        ``client.messages.stream(**kwargs)`` returns a context manager
        whose ``__enter__`` yields an iterable of zero events — enough
        to satisfy the ``_iter_with_cleanup`` shape without exercising
        actual streaming protocol.
        """
        captured: dict[str, object] = {}

        def stream(**kwargs: object) -> object:
            captured.update(kwargs)
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=iter([]))
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        client = MagicMock()
        client.messages.stream = stream
        return client, captured

    def _drive_session_through_anthropic(
        self,
        replay_flag: bool,
        msgs: list[dict[str, object]],
    ) -> dict[str, object]:
        """Run session._try_stream against a real AnthropicProvider with
        the resolver pre-set to *replay_flag*.  Returns the kwargs
        dict that reached the (mocked) Anthropic SDK boundary.
        """
        from turnstone.core.providers._anthropic import AnthropicProvider

        session = _make_session()
        session._registry = _registry_with_flag(replay=replay_flag)
        session._model_alias = "claude-opus-4-7"
        client, captured = self._stub_anthropic_client()
        real_provider = AnthropicProvider()
        with (
            patch.object(session, "_get_active_tools", return_value=None),
            patch.object(session, "_provider_extra_params", return_value=None),
            patch.object(session, "_get_deferred_names", return_value=frozenset()),
            patch.object(session, "_check_cancelled"),
        ):
            stream = session._try_stream(
                client=client,
                model="claude-opus-4-7",
                msgs=msgs,
                provider=real_provider,
                model_alias="claude-opus-4-7",
            )
            # Iterate the stream to drain the (empty) generator and ensure
            # _ensure_anthropic / convert / build_kwargs all ran.
            list(stream)
        return captured

    def test_replay_false_strips_thinking_at_wire(self) -> None:
        msgs: list[dict[str, object]] = [
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
        msgs: list[dict[str, object]] = [
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


class TestUtilityCompletionPassesFlag:
    """Non-streaming utility path (title gen, compaction, extraction) —
    same plumbing requirement as streaming."""

    def test_utility_completion_passes_resolved_flag(self) -> None:
        session = _make_session()
        session._registry = _registry_with_flag(replay=True)
        session._model_alias = "claude-opus-4-7"
        captured: dict[str, Any] = {}

        def capture_completion(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(content="title", finish_reason="stop", usage=None)

        mock_provider = MagicMock()
        mock_provider.create_completion = capture_completion
        session._provider = mock_provider
        with (
            patch.object(
                session, "_get_capabilities", return_value=SimpleNamespace(max_output_tokens=0)
            ),
            patch.object(session, "_provider_extra_params", return_value=None),
        ):
            session._utility_completion(
                messages=[{"role": "user", "content": "summarize"}],
                max_tokens=512,
                temperature=0.3,
            )
        assert captured["replay_reasoning_to_model"] is True
