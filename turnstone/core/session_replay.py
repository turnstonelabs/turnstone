"""Shared SSE replay preamble for workstream ``events`` connections.

Both interactive (``turnstone/server.py``) and coord
(``turnstone/console/server.py``) replays yield the same
``connected`` + optional ``status`` events at the top of their SSE
streams so per-tab status bars populate before any history arrives.
The kind-specific tail (interactive replays history; coord replays
pending approval / plan review) lives in each module's own
``_*_events_replay`` callback.

This module owns the shared preamble so a future field add lands
once instead of in two near-twin functions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turnstone.core.session import ChatSession


def session_replay_preamble(
    session: ChatSession | None,
    ui: Any,
) -> Iterable[dict[str, Any]]:
    """Yield ``connected`` + optional ``status`` events for an SSE replay.

    - Yields nothing when ``session`` is None — the close-then-reopen
      race can leave a workstream with a detached session; replay falls
      through to the kind-specific tail.
    - ``connected`` carries ``model`` / ``model_alias`` / ``skip_permissions``
      so the per-tab status bar populates the model cell before any
      history arrives.
    - ``status`` only fires when ``session._last_usage`` exists (a
      session that has completed at least one turn). The payload shape
      matches :meth:`SessionUI.on_status` so live ticks and replays use
      the same SSE event type.

    Pure-read — never mutates ``session`` / ``ui``.
    """
    if session is None:
        return

    yield {
        "type": "connected",
        "model": session.model,
        "model_alias": session.model_alias or "",
        "skip_permissions": getattr(ui, "auto_approve", False),
    }

    last_usage = session._last_usage
    if last_usage is None:
        return

    prompt_tok = last_usage.get("prompt_tokens", 0)
    completion_tok = last_usage.get("completion_tokens", 0)
    total_tok = prompt_tok + completion_tok
    cw = session.context_window or 0
    pct = total_tok / cw * 100 if cw > 0 else 0
    ws_lock = getattr(ui, "_ws_lock", None)
    if ws_lock is not None:
        with ws_lock:
            turn_tool_calls = getattr(ui, "_ws_turn_tool_calls", 0)
            turn_count = getattr(ui, "_ws_messages", 0)
    else:
        turn_tool_calls = getattr(ui, "_ws_turn_tool_calls", 0)
        turn_count = getattr(ui, "_ws_messages", 0)
    yield {
        "type": "status",
        "prompt_tokens": prompt_tok,
        "completion_tokens": completion_tok,
        "total_tokens": total_tok,
        "context_window": cw,
        "pct": round(pct, 1),
        "effort": session.reasoning_effort,
        "cache_creation_tokens": last_usage.get("cache_creation_tokens", 0),
        "cache_read_tokens": last_usage.get("cache_read_tokens", 0),
        "tool_calls_this_turn": turn_tool_calls,
        "turn_count": turn_count,
    }
