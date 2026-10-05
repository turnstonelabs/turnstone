"""Coordinator task writes present the coordinator session's owner lease."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._session_helpers import RecordingUI, make_session
from tests.test_coordinator_client import _make_read_client
from turnstone.core.storage import WorkstreamLeaseHeldError

_ITEMS: dict[str, dict[str, Any]] = {
    "add": {"title": "t", "status": "pending", "child_ws_id": None, "note": None},
    "update": {"task_id": "x", "title": None, "status": "done", "child_ws_id": None, "note": None},
    "remove": {"task_id": "x"},
    "reorder": {"task_ids": ["x"]},
}


@pytest.mark.parametrize("action", sorted(_ITEMS))
def test_the_tasks_tool_passes_the_sessions_fence(action: str) -> None:
    session = make_session(ui=RecordingUI(), ws_id="coord-1")
    session._coord_client = MagicMock()
    sentinel = object()
    session.write_fence = lambda: sentinel  # type: ignore[method-assign]

    session._exec_tasks({"call_id": "c1", "action": action, **_ITEMS[action]})

    call = getattr(session._coord_client, f"tasks_{action}")
    assert call.call_args.kwargs["lease"] is sentinel


@pytest.mark.parametrize("action", sorted(_ITEMS))
def test_a_task_write_without_the_lease_is_refused_while_it_is_held(
    tmp_path: Any, sqlite_backend_factory: Any, action: str
) -> None:
    storage = sqlite_backend_factory(str(tmp_path / "tasks.db"))
    storage.register_workstream(
        "coord-1", kind="coordinator", user_id="user-1", fork_reservation_token="tok"
    )
    grant = storage.acquire_workstream_lease(
        "coord-1",
        incarnation_token="tok",
        holder="console/1",
        node_id="console",
        ttl_seconds=30.0,
    )
    assert grant is not None
    client = _make_read_client(storage)
    first = client.tasks_add("coord-1", title="first", lease=grant.fence)
    second = client.tasks_add("coord-1", title="second", lease=grant.fence)
    write = {
        "add": lambda lease: client.tasks_add("coord-1", title="third", lease=lease),
        "update": lambda lease: client.tasks_update(
            "coord-1", task_id=first["id"], status="done", lease=lease
        ),
        "remove": lambda lease: client.tasks_remove("coord-1", task_id=first["id"], lease=lease),
        "reorder": lambda lease: client.tasks_reorder(
            "coord-1", task_ids=[second["id"], first["id"]], lease=lease
        ),
    }[action]
    before = client.tasks_get("coord-1")

    with pytest.raises(WorkstreamLeaseHeldError):
        write(None)
    assert client.tasks_get("coord-1") == before

    write(grant.fence)
    assert client.tasks_get("coord-1") != before
