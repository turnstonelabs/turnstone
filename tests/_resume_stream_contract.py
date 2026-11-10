"""The frames a coordinator's event stream sends right after a resume, for a stale cursor.

No browser test reaches the console's coordinator events route (the browser harness serves the
node app, whose events route never resumes), so this module is the one definition both halves
are held to: the console handler test asserts the real handler's frames against it, and the
pane test feeds the same frames to the real panes. A change to the sequence the server sends
fails the handler test until this module changes, and then the pane test runs on the new one.
"""

from __future__ import annotations

from typing import Any


def resumed_stream_frames(
    ws_id: str,
    *,
    lost_count: int,
    earliest_available_id: int,
    model: str = "gpt-test",
    model_alias: str = "default",
) -> list[dict[str, Any]]:
    """The events after the ``retry`` field, in order, as ``{"data": payload}``; none has an id.

    The envelope tells the client its cursor is not covered (the resumed session's replay ring
    is empty), then the replay of a freshly built coordinator: ``connected`` and the idle state.
    """
    return [
        {
            "data": {
                "type": "replay_truncated",
                "ws_id": ws_id,
                "lost_count": lost_count,
                "earliest_available_id": earliest_available_id,
            }
        },
        {
            "data": {
                "type": "connected",
                "model": model,
                "model_alias": model_alias,
                "project_name": "",
                "skip_permissions": False,
            }
        },
        {"data": {"type": "state_change", "state": "idle", "ws_id": ws_id}},
    ]
