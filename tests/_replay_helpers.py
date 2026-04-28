"""Shared mock factory for ``events_replay`` tests.

Both interactive (:func:`turnstone.server._interactive_events_replay`)
and coord (:func:`turnstone.console.server._coord_events_replay`) drive
the same shared preamble at
:func:`turnstone.core.session_replay.session_replay_preamble`.  Their
test suites share the underlying mock surface (session.model,
session.model_alias, session._last_usage, ui._pending_*, ui._ws_lock,
counters); this module is the single home for that shape so a future
field add lands once.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock


def make_replay_mocks(
    *,
    last_usage: dict[str, Any] | None = None,
    **ui_overrides: Any,
) -> tuple[Any, Any, Any]:
    """Build ``(ws, ui, request)`` MagicMocks for events-replay tests.

    Defaults match a fresh workstream that hasn't completed a turn
    (no ``last_usage``, no pending prompts).

    Args:
        last_usage: Sets ``ws.session._last_usage`` directly so tests
            don't have to reach into the nested mock; when ``None``
            (default), the status replay branch stays inert.
        **ui_overrides: Additional attributes set directly on the ``ui``
            mock (e.g. ``_pending_approval``, ``_pending_plan_review``,
            ``_llm_verdicts``, ``_ws_turn_tool_calls``, ``_ws_messages``).
    """
    session = MagicMock()
    session.model = "gpt-5"
    session.model_alias = "default"
    session._last_usage = last_usage
    session.context_window = 100000
    session.reasoning_effort = "medium"
    session.messages = []
    ui = MagicMock()
    ui.auto_approve = False
    ui._pending_approval = None
    ui._pending_plan_review = None
    ui._llm_verdicts = {}
    ui._ws_lock = threading.Lock()
    ui._ws_turn_tool_calls = 0
    ui._ws_messages = 0
    for key, value in ui_overrides.items():
        setattr(ui, key, value)
    ws = MagicMock()
    ws.session = session
    request = MagicMock()
    return ws, ui, request
