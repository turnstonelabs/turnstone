"""Shared test helpers — kept out of conftest.py since these are factories,
not fixtures, and several test files want to import them directly."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


def make_chat_session(**overrides: Any) -> Any:
    """Build a minimal ``ChatSession`` with sane test defaults.

    Caller passes any constructor arg as a kwarg to override the default —
    e.g. ``make_chat_session(memory_config=MemoryConfig(fetch_limit=5))``.
    """
    from turnstone.core.session import ChatSession

    defaults: dict[str, Any] = {
        "client": MagicMock(),
        "model": "test-model",
        "ui": MagicMock(),
        "instructions": None,
        "temperature": 0.5,
        "max_tokens": 4096,
        "tool_timeout": 30,
    }
    defaults.update(overrides)
    return ChatSession(**defaults)
