"""Audit-log discipline test for reasoning text.

Phase 1 of optional reasoning persistence surfaces stored thinking
blocks on the ``/history`` payload (UI rehydration). The bytes ride
through the helper (``extract_reasoning_for_history``) and the provider
extractor (``AnthropicProvider.extract_reasoning_text``).

This test pins the security-sensitive contract:

    Reasoning text MAY land on ``msg["reasoning"]`` (UI-bound),
    but MUST NOT appear in any ``Logger.info`` / ``warning`` /
    ``error`` payload at any layer in the pipeline.

The test mocks the standard-library ``logging.Logger`` info/warning/
error methods, runs a thinking-bearing turn through the relevant
extractors and history build, then asserts no captured log call's
positional args or kwargs contain the unique marker string. Replaces
the v4 grep-the-output approach (fragile when log strings are
formatted) with a structural mock-and-assert (tests the actual
contract rather than the rendered text).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests._session_helpers import make_session, replace_session_lane, scripted_provider
from turnstone.core.history_decoration import (
    extract_reasoning_for_history,
    extract_reasoning_text_from_provider_content,
)
from turnstone.core.providers._anthropic import AnthropicProvider
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
from turnstone.core.providers._protocol import StreamChunk, UsageInfo
from turnstone.core.trajectory import Turn

_MARKER = "SECRET_REASONING_MARKER_xyz123_unlikely_collision"


def _payload_contains_marker(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    """Walk a captured log call's args + kwargs for the marker string.

    Logger.info-style calls accept a format string + positional substitution
    args; the marker could appear in either the format string itself or
    the substitution values. Format-time strings (``%`` substitution) are
    NOT inspected because they're a stdlib formatting concern, not a
    callable our pipeline reaches into. The structural check is "no
    user-controlled marker appears in any arg slot we passed".
    """
    for a in args:
        if isinstance(a, str) and _MARKER in a:
            return True
        # Defensive — a list/dict/exception arg might carry the marker too.
        try:
            if _MARKER in repr(a):
                return True
        except Exception:
            continue
    for v in kwargs.values():
        if isinstance(v, str) and _MARKER in v:
            return True
        try:
            if _MARKER in repr(v):
                return True
        except Exception:
            continue
    return False


def _capture_log_calls():
    """Capture every Logger.info / warning / error call into a single list."""
    captured: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def make_recorder(level: str):
        def _rec(*args: Any, **kwargs: Any) -> None:
            captured.append((level, args, kwargs))

        return _rec

    return captured, [
        patch.object(logging.Logger, "info", side_effect=make_recorder("info"), autospec=True),
        patch.object(
            logging.Logger, "warning", side_effect=make_recorder("warning"), autospec=True
        ),
        patch.object(logging.Logger, "error", side_effect=make_recorder("error"), autospec=True),
    ]


class TestReasoningAuditLogDiscipline:
    """Reasoning text never lands at INFO+ severity on any logger."""

    def _thinking_msg(self, text: str = _MARKER) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": "Final answer.",
            "_provider_content": [
                {"type": "thinking", "thinking": text, "signature": "sig"},
                {"type": "text", "text": "Final answer."},
            ],
        }

    def test_anthropic_extractor_does_not_log_reasoning(self) -> None:
        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            provider = AnthropicProvider()
            text = provider.extract_reasoning_text(
                [{"type": "thinking", "thinking": _MARKER, "signature": "s"}]
            )
            assert text == _MARKER  # extractor IS allowed to return it
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], (
            f"AnthropicProvider.extract_reasoning_text leaked reasoning text "
            f"into INFO+ logs: {offending}"
        )

    def test_dispatch_helper_does_not_log_reasoning(self) -> None:
        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            text = extract_reasoning_text_from_provider_content(
                [{"type": "thinking", "thinking": _MARKER, "signature": "s"}]
            )
            assert text == _MARKER
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], (
            f"extract_reasoning_text_from_provider_content leaked reasoning "
            f"text into INFO+ logs: {offending}"
        )

    def test_list_helper_does_not_log_reasoning(self) -> None:
        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            messages = [self._thinking_msg(_MARKER)]
            extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
            assert messages[0]["reasoning"] == _MARKER  # UI-bound is allowed
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], (
            f"extract_reasoning_for_history leaked reasoning text into INFO+ logs: {offending}"
        )

    # ------------------------------------------------------------------
    # Phase 2 + Phase 3 surfaces — added in response to a code-review
    # finding that the original 4-test coverage missed every code path
    # introduced after Phase 1.  Each new test mirrors the structure
    # above: capture every Logger.info / warning / error call across
    # the operation, assert the marker doesn't appear in any captured
    # payload (UI-bound returns IS allowed; logging at INFO+ is NOT).
    # ------------------------------------------------------------------

    def test_openai_responses_extractor_does_not_log_reasoning(self) -> None:
        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            provider = OpenAIResponsesProvider()
            blocks = [
                {
                    "type": "reasoning",
                    "id": "r_1",
                    "summary": [{"type": "summary_text", "text": _MARKER}],
                }
            ]
            text = provider.extract_reasoning_text(blocks)
            assert _MARKER in text  # UI-bound return is allowed
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], (
            f"OpenAIResponsesProvider.extract_reasoning_text leaked reasoning "
            f"text into INFO+ logs: {offending}"
        )

    def test_openai_chat_extractor_does_not_log_reasoning(self) -> None:
        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            provider = OpenAIChatCompletionsProvider()
            blocks = [{"type": "reasoning_text", "text": _MARKER, "source": "vllm"}]
            text = provider.extract_reasoning_text(blocks)
            assert text == _MARKER
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], (
            f"OpenAIChatCompletionsProvider.extract_reasoning_text leaked "
            f"reasoning text into INFO+ logs: {offending}"
        )

    def test_synth_reasoning_block_via_stream_response_does_not_log_reasoning(
        self,
    ) -> None:
        """Drives session._stream_response (the real drain seam —
        _stream_attempt no longer exists post-#832; invokes
        model_turn.synth_reasoning_block at end-of-turn via
        finalize_provider_blocks) with a fake ``reasoning_delta=_MARKER``
        chunk; asserts no log call carried the marker text."""
        session = make_session()
        replace_session_lane(
            session,
            provider=scripted_provider(
                [
                    StreamChunk(reasoning_delta=_MARKER, is_first=True),
                    StreamChunk(content_delta="answer"),
                    StreamChunk(
                        finish_reason="stop",
                        usage=UsageInfo(
                            prompt_tokens=10,
                            completion_tokens=20,
                            total_tokens=30,
                        ),
                    ),
                ]
            ),
        )
        session.messages.append(Turn.user("hi"))
        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            result = session._stream_response(0)
            # Synth block stamped onto the native lane with the marker.
            assert result.turn.native is not None
            assert result.turn.native.blocks[0]["text"] == _MARKER
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], (
            f"_stream_response + synth_reasoning_block leaked reasoning "
            f"text into INFO+ logs: {offending}"
        )

    def test_anthropic_convert_messages_strip_does_not_log_reasoning(self) -> None:
        """Drives the Phase 2 strip predicate
        (``replay_reasoning_to_model=False``) which walks thinking
        blocks to filter them out before the wire payload is built;
        asserts no log call carried the marker text."""
        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            provider = AnthropicProvider()
            messages = [
                {
                    "role": "assistant",
                    "content": "Final answer.",
                    "_provider_content": [
                        {"type": "thinking", "thinking": _MARKER, "signature": "s"},
                        {"type": "text", "text": "Final answer."},
                    ],
                },
            ]
            _, converted = provider._convert_messages(messages, replay_reasoning_to_model=False)
            # Strip fired — thinking block dropped from wire.
            assistant = next(m for m in converted if m["role"] == "assistant")
            block_types = [b.get("type") for b in assistant["content"]]
            assert "thinking" not in block_types
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], (
            f"AnthropicProvider._convert_messages strip predicate leaked "
            f"reasoning text into INFO+ logs: {offending}"
        )

    def test_attach_vllm_chat_reasoning_field_does_not_log_reasoning(self) -> None:
        """Phase 5 surface — ``attach_vllm_chat_reasoning_field`` extracts
        persisted reasoning text and attaches it as a ``reasoning`` field
        on the outgoing assistant message dict.  The attached text is
        wire-bound (vLLM template render) and UI-bound (history rehydration
        already covered by Phase 1 tests above), but MUST NOT appear in
        any INFO+ log call along the way."""
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            messages = [self._thinking_msg(_MARKER)]
            out = attach_vllm_chat_reasoning_field(messages)
            # Wire-bound attach succeeded — marker IS allowed in the
            # returned dict's reasoning field.
            assert out[0]["reasoning"] == _MARKER
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], (
            f"attach_vllm_chat_reasoning_field leaked reasoning text into INFO+ logs: {offending}"
        )

    def test_maybe_attach_vllm_chat_reasoning_does_not_log_reasoning(self) -> None:
        """Phase 5 gate — ``model_turn.maybe_attach_vllm_chat_reasoning``.

        Post-#832 this composite gate is a plain module function (no
        ``ChatSession`` delegate survives; ``model_turn.model_turn`` calls
        it directly with the lane's own registry/alias). Pin that the
        gate path itself doesn't log reasoning text (the registry /
        capability lookups happen adjacent to the reasoning bytes; a
        defensive ``log.warning`` showing the message dict on an error
        path would silently violate the contract)."""
        from turnstone.core.model_turn import maybe_attach_vllm_chat_reasoning
        from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

        registry = SimpleNamespace(
            get_config=lambda _alias: SimpleNamespace(
                replay_reasoning_to_model=True,
                capabilities={},
                server_compat={"server_type": "vllm"},
            )
        )
        provider = OpenAIChatCompletionsProvider()

        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            out = maybe_attach_vllm_chat_reasoning(
                [self._thinking_msg(_MARKER)], provider, registry, "qwen3"
            )
            assert out[0]["reasoning"] == _MARKER
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], (
            f"model_turn.maybe_attach_vllm_chat_reasoning leaked reasoning "
            f"text into INFO+ logs: {offending}"
        )
