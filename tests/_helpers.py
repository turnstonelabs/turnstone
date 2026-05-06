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
    """Replace ``turnstone.core.session.get_storage`` with a minimal stub
    that exposes ``is_watch_active`` — the only storage surface the watch
    dispatch closure's ``valid_until`` predicate touches.

    Returns the list of ``watch_id``s the predicate was called with so
    callers can assert on call shape.  Use ``raise_on_is_active=True`` to
    pin the broad-except branch in the predicate.

    Tests that need a more elaborate storage stub (call tracking on
    multiple methods, return-shape variations beyond the active flag)
    should keep an inline class — this helper covers the common
    ``set up an active/inactive/boom storage stub`` shape that
    accumulated 7 near-duplicate ``monkeypatch.setattr`` sites.
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
