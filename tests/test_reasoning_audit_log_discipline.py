"""Audit-log discipline test for reasoning text.

Phase 1 of optional reasoning persistence surfaces stored thinking
blocks on the ``/history`` payload (UI rehydration). The bytes ride
through the helper (``extract_reasoning_for_history``), through the
provider extractor (``AnthropicProvider.extract_reasoning_text``), and
through the server build path (``_build_history``).

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

from turnstone.core.history_decoration import (
    extract_reasoning_for_history,
    extract_reasoning_text_from_provider_content,
)
from turnstone.core.providers._anthropic import AnthropicProvider
from turnstone.server import _build_history

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
            extract_reasoning_for_history(messages, persist_reasoning_flag=True)
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

    def test_build_history_does_not_log_reasoning(self) -> None:
        registry = SimpleNamespace(get_config=lambda alias: SimpleNamespace(persist_reasoning=True))
        session = SimpleNamespace(
            messages=[self._thinking_msg(_MARKER)],
            _ws_id="ws-audit",
            _registry=registry,
            _model_alias="claude-opus-4-7",
        )
        captured, patchers = _capture_log_calls()
        for p in patchers:
            p.start()
        try:
            with patch(
                "turnstone.server._load_verdict_indexes",
                return_value=({}, {}),
            ):
                history = _build_history(session)
            assert history[0]["reasoning"] == _MARKER  # UI-bound is allowed
        finally:
            for p in patchers:
                p.stop()
        offending = [
            (lvl, args, kwargs)
            for lvl, args, kwargs in captured
            if _payload_contains_marker(args, kwargs)
        ]
        assert offending == [], f"_build_history leaked reasoning text into INFO+ logs: {offending}"
