"""How a manager-hosted session holds its owner lease.

A slot holds one handle, for its own row. These tests drive a real
``SessionManager`` and real ``ChatSession`` objects against the storage
backend ``--storage-backend`` selects, and check behavior rather than
containers: which slot retires, which session stops, which rows land, and who
holds each row afterwards. Another process is simulated by taking a row's lease
with a second holder.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from tests._session_helpers import RecordingUI, make_session
from tests._storage_fakes import expire_lease as _expire
from tests.test_session_manager import FakeAdapter, _make_manager
from turnstone.core.session import ConversationPersistenceError
from turnstone.core.session_manager import SessionFactoryLeaseError
from turnstone.core.storage import (
    WorkstreamLeaseHeldError,
    WorkstreamLeaseLostError,
)
from turnstone.core.storage._schema import workstreams
from turnstone.core.workstream import Workstream, WorkstreamHistoryUnavailableError, WorkstreamState
from turnstone.core.workstream_lease import WorkstreamLease


class _RealSessionAdapter(FakeAdapter):
    """Builds real ``ChatSession`` objects that present the manager's lease."""

    def build_session(self, ws: Workstream, **kwargs: object) -> Any:
        self.build_session_calls += 1
        session = make_session(
            ui=RecordingUI(),
            ws_id=ws.id,
            workstream_lease=kwargs.get("workstream_lease"),
            fork_reservation_token=kwargs.get("fork_reservation_token"),
        )
        self.built_sessions.append(session)
        return session


def _host(storage: Any) -> tuple[Any, _RealSessionAdapter]:
    adapter = _RealSessionAdapter()
    mgr, _, _ = _make_manager(adapter, storage=storage, node_id="node-a")
    return mgr, adapter


def _steal(storage: Any, ws_id: str) -> Any:
    """Another process takes ``ws_id`` over once our lease has expired."""
    _expire(storage, ws_id)
    grant = storage.acquire_workstream_lease(
        ws_id,
        incarnation_token=storage.get_workstream_reservation_token(ws_id),
        holder="node-b/other",
        node_id="node-b",
        ttl_seconds=30.0,
    )
    assert grant is not None
    return grant


def _raw_lease(storage: Any, ws_id: str) -> tuple[str | None, int]:
    """The stored holder and epoch, whatever the lease's expiry."""
    with storage._engine.connect() as conn:
        row = conn.execute(
            sa.select(workstreams.c.lease_holder, workstreams.c.lease_epoch).where(
                workstreams.c.ws_id == ws_id
            )
        ).one()
    return row.lease_holder, int(row.lease_epoch)


def _seed(storage: Any, ws_id: str) -> str:
    """An existing workstream with history, nobody holding it."""
    assert storage.register_workstream(ws_id, fork_reservation_token=f"tok-{ws_id}") is True
    storage.save_message(ws_id, "user", f"history of {ws_id}")
    return ws_id


def _tracked_ids(mgr: Any) -> set[str]:
    return {lease.ws_id for lease in mgr._lease_keeper.tracked()}


def _admit(session: Any, key: str, persist: Any) -> Any:
    """Admit one row into the session's conversation journal."""
    with session._history_handoff_lock:
        return session._journal_conversation_row_locked(
            commit_key=key,
            message={"role": "assistant", "content": key},
            persist=persist,
            event_id=None,
        )


@pytest.mark.parametrize("path", ["create", "open"])
def test_a_factory_that_drops_the_lease_fails_the_load(storage_backend: Any, path: str) -> None:
    """A session built without the slot's lease would have every write refused."""

    class _Dropping(_RealSessionAdapter):
        def build_session(self, ws: Workstream, **kwargs: object) -> Any:
            kwargs.pop("workstream_lease", None)
            return super().build_session(ws, **kwargs)

    mgr, _, _ = _make_manager(_Dropping(), storage=storage_backend, node_id="node-a")
    target = _seed(storage_backend, "ws-dropped") if path == "open" else ""

    with pytest.raises(SessionFactoryLeaseError, match="did not pass workstream_lease"):
        if path == "create":
            mgr.create(user_id="u1")
        else:
            mgr.open(target)

    assert mgr.list_all() == []
    assert _tracked_ids(mgr) == set()
    if path == "open":
        assert storage_backend.get_workstream(target)["lease_node_id"] is None


# -- losing a lease -----------------------------------------------------------


def test_losing_the_lease_retires_the_slot_once_and_stops_the_session(
    storage_backend: Any,
) -> None:
    mgr, adapter = _host(storage_backend)
    ws = mgr.create(user_id="u1")
    session = ws.session

    _steal(storage_backend, ws.id)
    lost = mgr.renew_leases_once()

    assert [lease.ws_id for lease in lost] == [ws.id]
    assert mgr.get(ws.id) is None
    assert session._workstream_lease_lost
    assert adapter.cleaned_up.count(ws.id) == 1
    assert mgr.renew_leases_once() == []
    assert adapter.cleaned_up.count(ws.id) == 1
    # Silent: the workstream lives on in the other process.
    assert all(event.ws_id != ws.id for event in adapter.events_of("closed"))
    assert adapter.lease_retired == [ws.id]


def test_a_slot_whose_lease_was_taken_keeps_its_state_writes_fenced(
    storage_backend: Any,
) -> None:
    mgr, _ = _host(storage_backend)
    ws = mgr.create(user_id="u1")
    grant = _steal(storage_backend, ws.id)
    # Before any renewal tick, the new owner's lease is momentarily expired:
    # an unfenced write would land and fence it out.
    _expire(storage_backend, ws.id)

    mgr.set_state(ws.id, WorkstreamState.RUNNING)

    assert _raw_lease(storage_backend, ws.id) == (grant.fence.holder, grant.fence.epoch)
    assert storage_backend.get_workstream(ws.id)["state"] != "running"


def test_a_copy_that_found_its_lease_lost_is_retired_when_reopened(
    storage_backend: Any,
) -> None:
    mgr, _ = _host(storage_backend)
    target = _seed(storage_backend, "ws-inv-supersede")
    copy = mgr.open(target)
    assert copy is not None
    grant = _steal(storage_backend, target)

    def refused() -> int:
        raise WorkstreamLeaseLostError(target)

    # The copy's next save is refused: the session stops itself.
    copy.session._persist_pending_conversation_commit(_admit(copy.session, "t-row", refused))
    assert copy.session._workstream_lease_lost
    # Before any renewal tick, the other process lets go and the target is
    # opened here again: the stopped copy is retired, never served.
    storage_backend.release_workstream_lease(grant.fence)
    reopened = mgr.open(target)
    assert reopened is not None and reopened is not copy

    mgr.renew_leases_once()

    assert mgr.get(target) is reopened
    assert storage_backend.get_workstream(target)["lease_node_id"] == "node-a"


def test_a_write_that_dropped_its_fence_stops_the_session_loudly(storage_backend: Any) -> None:
    """Storage refuses an unfenced row while the lease is live: never a silent retry loop."""
    mgr, _ = _host(storage_backend)
    ws = mgr.create(user_id="u1")
    session = ws.session

    def held() -> int:
        # Only a write that dropped its fence can meet a live lease this way.
        raise WorkstreamLeaseHeldError(ws.id, holder_node_id="node-z")

    session._persist_pending_conversation_commit(_admit(session, "held-row", held))

    assert session._workstream_lease_lost
    assert session.is_workstream_gone()
    assert "held-row" not in session._pending_conversation_commits


def test_a_refused_soft_close_does_not_undo_a_lease_loss_stop(storage_backend: Any) -> None:
    """A close refused for unsaved rows rolls back only its own latch, never a terminal stop."""
    from unittest.mock import patch

    mgr, _ = _host(storage_backend)
    session = mgr.create(user_id="u1").session

    def flapping() -> int:
        raise OSError("storage flap")

    with pytest.raises(ConversationPersistenceError):
        session._persist_pending_conversation_commit(
            _admit(session, "k1", flapping), _retry_admitted=True
        )
    assert session.has_unresolved_conversation_persistence()
    _steal(storage_backend, session._ws_id)
    original_cancel = session.cancel

    def cancel_after_loss(*args: Any, **kwargs: Any) -> Any:
        # The lease-loss stop lands while the close is preparing.
        session.handle_workstream_lease_lost()
        return original_cancel(*args, **kwargs)

    with patch.object(session, "cancel", side_effect=cancel_after_loss):
        assert session.prepare_soft_close() is False

    assert session._publication_shutdown is True
    assert session._cancel_event.is_set()
    with pytest.raises(RuntimeError, match="closed session"):
        session._capture_worker_claim()


# -- opening ------------------------------------------------------------------


def test_an_open_whose_history_read_failed_is_refused_and_lets_go(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed history read is never served as an empty history."""
    mgr, _ = _host(storage_backend)
    target = _seed(storage_backend, "ws-blip")

    def blip(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("storage blip")

    monkeypatch.setattr(storage_backend, "load_message_turns", blip)

    with pytest.raises(WorkstreamHistoryUnavailableError):
        mgr.open(target)

    assert mgr.get(target) is None
    assert _tracked_ids(mgr) == set()
    assert storage_backend.get_workstream(target)["lease_node_id"] is None
    monkeypatch.undo()
    reopened = mgr.open(target)
    assert reopened is not None
    assert [turn.text for turn in reopened.session.messages] == [f"history of {target}"]


class _NodeSessionAdapter(_RealSessionAdapter):
    """Real sessions on node-a, so a load ends other nodes' watches."""

    def build_session(self, ws: Workstream, **kwargs: object) -> Any:
        self.build_session_calls += 1
        session = make_session(
            ui=RecordingUI(),
            ws_id=ws.id,
            node_id="node-a",
            workstream_lease=kwargs.get("workstream_lease"),
            fork_reservation_token=kwargs.get("fork_reservation_token"),
        )
        self.built_sessions.append(session)
        return session


def test_an_open_whose_lease_moves_during_the_load_is_refused_and_ends_no_watch(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy is not served as live, and the watch stays with the process now holding it."""
    mgr, _, _ = _make_manager(_NodeSessionAdapter(), storage=storage_backend, node_id="node-a")
    target = _seed(storage_backend, "ws-moved-mid-load")
    storage_backend.create_watch(
        watch_id="w-other-node",
        ws_id=target,
        node_id="node-z",
        name="build",
        command="make check",
        interval_secs=60,
        stop_on=None,
        max_polls=5,
        created_by="model",
        next_poll="2099-01-01T00:00:00",
    )
    end_watches = storage_backend.end_foreign_node_watches

    def taken_over_first(ws_id: str, node_id: str, *, lease: Any = None) -> Any:
        # Another process takes the workstream over while this one loads it.
        grant = storage_backend.acquire_workstream_lease(
            ws_id,
            incarnation_token=f"tok-{ws_id}",
            holder="node-z/9",
            node_id="node-z",
            ttl_seconds=30.0,
        )
        assert grant is not None
        return end_watches(ws_id, node_id, lease=lease)

    monkeypatch.setattr(storage_backend, "end_foreign_node_watches", taken_over_first)

    with pytest.raises(WorkstreamLeaseLostError):
        mgr.open(target)

    assert mgr.get(target) is None
    assert storage_backend.get_watch("w-other-node")["active"]


def _blip(_ws_id: str) -> dict[str, str]:
    raise RuntimeError("storage blip")


def test_a_load_whose_config_read_failed_is_refused_not_run_on_defaults(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable configuration is never taken for an empty one."""
    target = _seed(storage_backend, "ws-config-blip")
    storage_backend.save_workstream_config(target, {"temperature": "0.3"})
    session = make_session(ui=RecordingUI(), ws_id=target)
    monkeypatch.setattr(storage_backend, "load_workstream_config", _blip)

    with pytest.raises(WorkstreamHistoryUnavailableError):
        session.rehydrate()
    assert session.messages == []


def test_a_session_built_to_load_while_the_config_read_fails_saves_nothing_over_it(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Construction saves its settings only when none are saved; a failed read is not "none"."""
    target = _seed(storage_backend, "ws-config-build-blip")
    storage_backend.save_workstream_config(target, {"temperature": "0.3"})
    grant = storage_backend.acquire_workstream_lease(
        target,
        incarnation_token=f"tok-{target}",
        holder="node-a/1",
        node_id="node-a",
        ttl_seconds=30.0,
    )
    assert grant is not None
    read = storage_backend.load_workstream_config
    monkeypatch.setattr(storage_backend, "load_workstream_config", _blip)
    mcp = MagicMock()
    mcp.get_tools.return_value = []

    with pytest.raises(WorkstreamHistoryUnavailableError):
        make_session(
            ui=RecordingUI(),
            ws_id=target,
            workstream_lease=WorkstreamLease(grant.fence),
            mcp_client=mcp,
        )
    assert read(target)["temperature"] == "0.3"
    # It raised before registering anywhere: nothing holds the half-built session.
    assert mcp.add_listener.call_count == 0
    assert mcp.add_resource_listener.call_count == 0
    assert mcp.add_prompt_listener.call_count == 0


def test_an_open_whose_settings_cannot_be_read_is_told_to_retry(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The manager's own settings read raises the load's refusal (HTTP 503), not the raw error,
    and logs its cause (the 503 carries none)."""
    mgr, _ = _host(storage_backend)
    target = _seed(storage_backend, "ws-manager-config-blip")
    monkeypatch.setattr(storage_backend, "load_workstream_config", _blip)

    with pytest.raises(WorkstreamHistoryUnavailableError):
        mgr.open(target)

    assert mgr.get(target) is None
    assert any("session_mgr.config_read_failed" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("finalize", ["refused", "raises"])
def test_a_create_that_fails_its_last_step_leaves_no_mcp_registration(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch, finalize: str
) -> None:
    """Construction fails after registering with the MCP client: nothing keeps the half-built
    session."""
    target = _seed(storage_backend, "ws-finalize-fails")
    grant = storage_backend.acquire_workstream_lease(
        target,
        incarnation_token=f"tok-{target}",
        holder="node-a/1",
        node_id="node-a",
        ttl_seconds=30.0,
    )
    assert grant is not None

    def finalize_deferred_create(*_args: Any, **_kwargs: Any) -> bool:
        if finalize == "raises":
            raise OSError("storage blip")
        return False

    monkeypatch.setattr(storage_backend, "finalize_deferred_create", finalize_deferred_create)
    mcp = MagicMock()
    mcp.get_tools.return_value = []

    with pytest.raises(RuntimeError if finalize == "refused" else OSError):
        make_session(
            ui=RecordingUI(),
            ws_id=target,
            workstream_lease=WorkstreamLease(grant.fence),
            fork_reservation_token=f"tok-{target}",
            mcp_client=mcp,
        )

    for added, removed in (
        (mcp.add_listener, mcp.remove_listener),
        (mcp.add_resource_listener, mcp.remove_resource_listener),
        (mcp.add_prompt_listener, mcp.remove_prompt_listener),
    ):
        assert added.call_count == 1
        assert removed.call_args.args == added.call_args.args


def test_a_session_built_without_a_lease_still_builds_when_the_config_read_fails(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eval and optimizer runs build throwaway sessions that load nothing; storage trouble
    must not stop them."""
    monkeypatch.setattr(storage_backend, "load_workstream_config", _blip)

    assert make_session(ui=RecordingUI(), ws_id="ws-throwaway").ws_id == "ws-throwaway"


def test_an_open_of_rows_that_rebuild_to_no_turns_serves_it_empty(storage_backend: Any) -> None:
    """Stored rows recovery drops entirely (a first tool call left half answered) open empty."""
    import json

    mgr, _ = _host(storage_backend)
    assert storage_backend.register_workstream("ws-half", fork_reservation_token="tok-ws-half")
    calls = [
        {"id": call_id, "type": "function", "function": {"name": "bash", "arguments": "{}"}}
        for call_id in ("a", "b")
    ]
    storage_backend.save_message("ws-half", "assistant", "", tool_calls=json.dumps(calls))
    storage_backend.save_message("ws-half", "tool", "out-a", tool_call_id="a")
    assert storage_backend.load_message_turns("ws-half") == []

    opened = mgr.open("ws-half")

    assert opened is not None and opened.session.messages == []


def test_an_open_with_no_turns_ends_other_nodes_watches_untold_and_leaves_titling_alone(
    storage_backend: Any,
) -> None:
    """The new holder ends the watch; no notice fills the workstream, so a watch restore still sees
    it empty."""
    mgr, _, _ = _make_manager(_NodeSessionAdapter(), storage=storage_backend, node_id="node-a")
    assert storage_backend.register_workstream("ws-empty-watch", fork_reservation_token="tok-ew")
    storage_backend.create_watch(
        watch_id="w-empty-other",
        ws_id="ws-empty-watch",
        node_id="node-z",
        name="build",
        command="make check",
        interval_secs=60,
        stop_on=None,
        max_polls=5,
        created_by="model",
        next_poll="2099-01-01T00:00:00",
    )

    opened = mgr.open("ws-empty-watch")

    assert opened is not None and opened.session.messages == []
    assert not storage_backend.get_watch("w-empty-other")["active"]
    assert storage_backend.load_message_turns("ws-empty-watch") == []
    assert opened.session._title_generated is False  # the first exchange still titles it


def test_an_open_of_a_workstream_with_no_turns_serves_it(storage_backend: Any) -> None:
    """With its saved settings: an empty workstream is not a fresh session."""
    mgr, _ = _host(storage_backend)
    assert storage_backend.register_workstream("ws-empty", fork_reservation_token="tok-ws-empty")
    storage_backend.save_workstream_config(
        "ws-empty", {"temperature": "0.3", "reasoning_effort": "high"}
    )

    opened = mgr.open("ws-empty")

    assert opened is not None
    session = opened.session
    assert session.messages == []
    assert storage_backend.get_workstream("ws-empty")["lease_node_id"] == "node-a"
    assert (session.temperature, session.reasoning_effort) == (0.3, "high")
    assert session._fork_reservation_token == "tok-ws-empty"
    # The next settings save keeps them rather than writing defaults over them.
    session._save_config()
    saved = storage_backend.load_workstream_config("ws-empty")
    assert (saved["temperature"], saved["reasoning_effort"]) == ("0.3", "high")


# -- shutdown -----------------------------------------------------------------


def test_the_shutdown_drain_gives_up_at_its_deadline(storage_backend: Any) -> None:
    import time

    mgr, _ = _host(storage_backend)
    idle = mgr.create(user_id="u1").session
    assert idle.shutdown_publication_and_drain_durability(timeout=0.5) is True
    stuck = mgr.create(user_id="u1").session
    # An admitted durability batch that never completes.
    stuck._durability_next_ticket += 1

    started = time.monotonic()
    assert stuck.shutdown_publication_and_drain_durability(timeout=0.2) is False
    assert time.monotonic() - started < 2.0


def test_shutdown_keeps_the_lease_of_a_session_that_does_not_drain(storage_backend: Any) -> None:
    """Releasing under a write still in flight would refuse that write: it waits out the TTL."""
    mgr, _ = _host(storage_backend)
    drained = mgr.create(user_id="u1")
    stuck = mgr.create(user_id="u1")
    stuck.session._durability_next_ticket += 1
    mgr._SHUTDOWN_DRAIN_SECONDS = 0.3

    mgr.release_leases()

    assert storage_backend.get_workstream(drained.id)["lease_node_id"] is None
    assert storage_backend.get_workstream(stuck.id)["lease_node_id"] == "node-a"


def test_a_leased_sessions_writes_land(storage_backend: Any) -> None:
    """Each kind of write a manager-hosted session makes presents its lease."""
    from tests._session_helpers import arm_session
    from turnstone.core.providers import StreamChunk

    mgr, _ = _host(storage_backend)
    ws = mgr.create(user_id="u1")
    session = ws.session
    arm_session(session, iter([StreamChunk(content_delta="Hello", finish_reason="stop")]))

    session.send("hi")
    session._append_system_turn(
        "watch_triggered",
        "build finished",
        watch_id="watch-1",
        watch_name="build",
        command="make",
        output="ok",
        poll_count=1,
        max_polls=5,
        is_final=True,
    )
    session._save_config()
    mgr.set_state(ws.id, WorkstreamState.RUNNING)

    roles = [row["role"] for row in storage_backend.load_messages(ws.id)]
    assert roles[:2] == ["user", "assistant"]
    assert "system" in roles
    assert storage_backend.get_workstream(ws.id)["state"] == "running"
    assert storage_backend.load_workstream_config(ws.id) != {}
    assert not session._workstream_lease_lost
    assert not session.is_workstream_gone()
