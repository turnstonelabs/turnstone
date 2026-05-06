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


def patch_session_storage(
    monkeypatch: Any,
    *,
    active: bool = True,
    raise_on_is_active: bool = False,
) -> list[str]:
    """Patch ``session.get_storage`` to a stub whose ``is_watch_active``
    returns *active* (or raises if *raise_on_is_active*).  Returns the
    list of ``watch_id``s the predicate was called with.
    """
    from turnstone.core import session as session_mod

    calls: list[str] = []

    class _Stub:
        def is_watch_active(self, watch_id: str) -> bool:
            calls.append(watch_id)
            if raise_on_is_active:
                raise RuntimeError("storage down")
            return active

    monkeypatch.setattr(session_mod, "get_storage", lambda: _Stub())
    return calls
