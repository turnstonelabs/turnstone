"""Shared session-test helpers.

Two reasoning-test modules (``test_session_replay_reasoning.py`` and
``test_session_synth_reasoning_block.py``) need the same minimal
``ChatSession`` factory + a ``SessionUIBase`` no-op subclass.  Hoisting
keeps a future third caller from drifting on the defaults — the third
existing ``_make_session`` (``test_model_registry.py``) deliberately
takes a different signature (registry / model_alias / reasoning_effort
+ ``_FakeUI``) and is NOT a candidate for sharing this helper.

Module is named with a leading underscore so pytest doesn't try to
collect it as a test file — it's an importable utility, not a test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from turnstone.core.session import ChatSession
from turnstone.core.session_ui_base import SessionUIBase


class NullUI(SessionUIBase):
    """Bare-bones UI satisfying the SessionUIBase contract for tests
    that don't care about UI side effects."""

    def __init__(self) -> None:
        super().__init__()


def make_session(**kwargs: Any) -> ChatSession:
    """Build a ChatSession with minimal defaults; tests override
    individual fields via kwargs."""
    defaults: dict[str, Any] = {
        "client": MagicMock(),
        "model": "test-model",
        "ui": NullUI(),
        "instructions": None,
        "temperature": 0.5,
        "max_tokens": 4096,
        "tool_timeout": 30,
    }
    defaults.update(kwargs)
    return ChatSession(**defaults)


def mock_completion_result(
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """A provider result shaped like ``CompletionResult``.

    Callers that route through ``model_turn`` (judges, task agents, and
    every lane #827 migrates) hit its re-ingest, which iterates
    ``tool_calls``/``provider_blocks`` and joins ``reasoning`` — a bare
    MagicMock attribute would TypeError deep inside the seam, so every
    field the re-ingest reads is pinned to a real value here.  ONE shared
    definition: when the re-ingest starts reading a new CompletionResult
    field, add it here and every suite moves together.
    """
    result = MagicMock()
    result.content = content
    result.tool_calls = tool_calls
    result.finish_reason = "stop"
    result.usage = None
    result.provider_blocks = []
    result.reasoning = ""
    return result
