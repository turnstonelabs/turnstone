"""Owner-lease acceptance scenarios against the real storage backends.

The contention cases need PostgreSQL semantics (SQLite takes a live lease over
by contract) and skip on SQLite; the rest run on whichever backend
``--storage-backend`` selects. Managers use the FakeAdapter so these tests
exercise the manager's real storage calls; the session cases drive a real
``ChatSession``, through a scripted provider where a turn is involved.
"""

from __future__ import annotations

import contextlib
import functools
from typing import Any

import pytest

from tests._session_helpers import RecordingUI, arm_session, make_session
from tests._storage_fakes import expire_lease as _expire
from tests.test_session_manager import FakeAdapter, _make_manager
from turnstone.core.providers import StreamChunk
from turnstone.core.session import GenerationCancelled
from turnstone.core.storage import WorkstreamLeaseHeldError, WorkstreamLeaseLostError
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.workstream import WorkstreamState
from turnstone.core.workstream_lease import WorkstreamLease


def _manager(storage: Any, node_id: str) -> tuple[Any, FakeAdapter]:
    adapter = FakeAdapter()
    mgr, _, _ = _make_manager(adapter, storage=storage, node_id=node_id)
    return mgr, adapter


def _require_postgresql(storage: Any) -> None:
    if isinstance(storage, SQLiteBackend):
        pytest.skip("SQLite takes a live lease over by contract")


def test_two_servers_cannot_both_open_one_incarnation(storage_backend: Any) -> None:
    _require_postgresql(storage_backend)
    owner, _ = _manager(storage_backend, "node-a")
    other, _ = _manager(storage_backend, "node-b")
    ws = owner.create(user_id="u1")

    with pytest.raises(WorkstreamLeaseHeldError) as excinfo:
        other.open(ws.id)
    assert excinfo.value.holder_node_id == "node-a"
    assert other.count == 0

    assert owner.close(ws.id)
    assert other.open(ws.id) is not None
    assert storage_backend.get_workstream(ws.id)["lease_node_id"] == "node-b"


def test_expired_owner_is_taken_over_and_retired(storage_backend: Any) -> None:
    stale, stale_adapter = _manager(storage_backend, "node-a")
    successor, _ = _manager(storage_backend, "node-b")
    ws = stale.create(user_id="u1")
    stale.set_state(ws.id, WorkstreamState.RUNNING)
    _expire(storage_backend, ws.id)

    taken = successor.open(ws.id)
    assert taken is not None
    successor.set_state(ws.id, WorkstreamState.ATTENTION)

    # The paused former owner can no longer write its row...
    stale.set_state(ws.id, WorkstreamState.IDLE)
    assert storage_backend.get_workstream(ws.id)["state"] == "attention"
    # ...and its next renewal finds the loss and retires the copy silently.
    lost = stale.renew_leases_once()
    assert [lease.ws_id for lease in lost] == [ws.id]
    assert stale.get(ws.id) is None
    assert all(event.ws_id != ws.id for event in stale_adapter.events_of("closed"))
    assert storage_backend.get_workstream(ws.id)["lease_node_id"] == "node-b"


def test_non_holder_delete_refused_while_owner_holds_and_owner_delete_succeeds(
    storage_backend: Any,
) -> None:
    _require_postgresql(storage_backend)
    owner, _ = _manager(storage_backend, "node-a")
    other, _ = _manager(storage_backend, "node-b")
    ws = owner.create(user_id="u1")
    token = storage_backend.get_workstream_reservation_token(ws.id)
    delete_exact = functools.partial(
        storage_backend.delete_workstream_if_fork_reserved, ws.id, token
    )

    with pytest.raises(WorkstreamLeaseHeldError):
        other.delete_persisted(ws.id, delete_fn=delete_exact)
    assert storage_backend.get_workstream(ws.id) is not None
    assert owner.get(ws.id) is ws

    assert owner.delete_persisted(ws.id, delete_fn=delete_exact) is True
    assert storage_backend.get_workstream(ws.id) is None
    assert owner.get(ws.id) is None


def test_lease_lost_during_a_turn_stops_the_turn_and_persists_nothing(
    storage_backend: Any,
) -> None:
    ws_id = "ws-turn-loss"
    token = "turn-loss-incarnation"
    assert storage_backend.register_workstream(ws_id, fork_reservation_token=token) is True
    grant = storage_backend.acquire_workstream_lease(
        ws_id,
        incarnation_token=token,
        holder="node-a/1",
        node_id="node-a",
        ttl_seconds=30.0,
    )
    assert grant is not None
    lease = WorkstreamLease(grant.fence)
    ui = RecordingUI()
    session = make_session(ui=ui, ws_id=ws_id, workstream_lease=lease)
    successor: list[Any] = []

    def stream_with_takeover() -> Any:
        yield StreamChunk(content_delta="Hello ")
        # The owner stalls mid-turn long enough to lose its lease, and
        # another server takes the workstream over.
        _expire(storage_backend, ws_id)
        successor.append(
            storage_backend.acquire_workstream_lease(
                ws_id,
                incarnation_token=token,
                holder="node-b/2",
                node_id="node-b",
                ttl_seconds=30.0,
            )
        )
        yield StreamChunk(content_delta="world", finish_reason="stop")

    arm_session(session, stream_with_takeover())
    with contextlib.suppress(GenerationCancelled):
        session.send("hello")

    assert successor and successor[0] is not None
    # Only the user row admitted before the takeover is durable; the stale
    # owner's assistant row was refused.
    rows = storage_backend.load_messages(ws_id)
    assert [row["role"] for row in rows] == ["user"]
    assert lease.state == "lost"
    assert session.is_workstream_gone()
    assert any("another process" in info for info in ui.of("info"))
    with pytest.raises(WorkstreamLeaseLostError):
        storage_backend.update_workstream_state(ws_id, "running", lease=lease.fence)
    # The new owner keeps writing.
    storage_backend.save_message(
        ws_id, "assistant", "from the new owner", commit_key="successor", lease=successor[0].fence
    )
    assert [row["role"] for row in storage_backend.load_messages(ws_id)] == ["user", "assistant"]


def test_remote_delete_after_expiry_refuses_the_owners_in_flight_row(
    storage_backend: Any,
) -> None:
    """A paused owner's tail cannot land once another server deleted the workstream."""
    ws_id = "ws-delete-tail"
    token = "delete-tail-incarnation"
    assert storage_backend.register_workstream(ws_id, fork_reservation_token=token) is True
    grant = storage_backend.acquire_workstream_lease(
        ws_id,
        incarnation_token=token,
        holder="node-a/1",
        node_id="node-a",
        ttl_seconds=30.0,
    )
    assert grant is not None
    session = make_session(
        ui=RecordingUI(), ws_id=ws_id, workstream_lease=WorkstreamLease(grant.fence)
    )
    deleted: list[bool] = []

    def stream_with_remote_delete() -> Any:
        yield StreamChunk(content_delta="Hello ")
        # The owner stalls past its lease, and another server deletes the
        # workstream: an unfenced delete proceeds on an expired lease.
        _expire(storage_backend, ws_id)
        deleted.append(bool(storage_backend.delete_workstream(ws_id)))
        yield StreamChunk(content_delta="world", finish_reason="stop")

    arm_session(session, stream_with_remote_delete())
    with contextlib.suppress(GenerationCancelled):
        session.send("hello")

    assert deleted == [True]
    assert storage_backend.get_workstream(ws_id) is None
    assert storage_backend.load_messages(ws_id) == []
    assert session.is_workstream_gone()


def test_opening_a_workstream_ends_watches_bound_to_another_node(storage_backend: Any) -> None:
    """A watch runs only on its node; the new owner ends it and tells the model once."""
    ws_id = "ws-watch-owner-change"
    assert storage_backend.register_workstream(ws_id) is True
    storage_backend.save_message(ws_id, "user", "keep an eye on the build")
    for watch_id, node in (("watch-foreign", "node-a"), ("watch-local", "node-b")):
        storage_backend.create_watch(
            watch_id=watch_id,
            ws_id=ws_id,
            node_id=node,
            name=f"build-{node}",
            command="make check",
            interval_secs=60.0,
            stop_on=None,
            max_polls=10,
            created_by="owner",
            next_poll="2026-01-01T00:00:00",
        )
    ui = RecordingUI()
    session = make_session(ui=ui, ws_id=ws_id, node_id="node-b")

    assert session.rehydrate() is True

    assert storage_backend.is_watch_active("watch-foreign") is False
    assert storage_backend.is_watch_active("watch-local") is True
    notices = [
        row
        for row in storage_backend.load_messages(ws_id)
        if row["role"] == "system" and row.get("_source") == "watch_triggered"
    ]
    assert len(notices) == 1
    assert "build-node-a" in notices[0]["content"]
    assert "ownership of this workstream moved" in notices[0]["content"]

    # Reopening on the same node keeps its own watches and adds no notice.
    reopened = make_session(ui=RecordingUI(), ws_id=ws_id, node_id="node-b")
    assert reopened.rehydrate() is True
    assert storage_backend.is_watch_active("watch-local") is True
    assert (
        len(
            [
                row
                for row in storage_backend.load_messages(ws_id)
                if row["role"] == "system" and row.get("_source") == "watch_triggered"
            ]
        )
        == 1
    )


def _foreign_watch(
    storage: Any, ws_id: str, *, watch_id: str = "watch-foreign", name: str = "build"
) -> None:
    """A saved workstream with history and a watch bound to node-a."""
    assert storage.register_workstream(ws_id) is True
    storage.save_message(ws_id, "user", "keep an eye on the build")
    storage.create_watch(
        watch_id=watch_id,
        ws_id=ws_id,
        node_id="node-a",
        name=name,
        command="make check",
        interval_secs=60.0,
        stop_on=None,
        max_polls=10,
        created_by="owner",
        next_poll="2026-01-01T00:00:00",
    )


def _ownership_notices(storage: Any, ws_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in storage.load_messages(ws_id)
        if row["role"] == "system" and row.get("_source") == "watch_triggered"
    ]


def test_a_watch_whose_notice_fails_still_ends(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watch ends first; its notice is best effort."""
    ws_id = "ws-watch-notice-fails"
    _foreign_watch(storage_backend, ws_id)
    session = make_session(ui=RecordingUI(), ws_id=ws_id, node_id="node-b")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(session, "_append_system_turn", refuse)

    assert session.rehydrate() is True
    assert storage_backend.is_watch_active("watch-foreign") is False
    assert _ownership_notices(storage_backend, ws_id) == []


def test_the_ownership_notice_sanitizes_the_watch_name(storage_backend: Any) -> None:
    ws_id = "ws-watch-hostile-name"
    override = chr(0x202E)
    _foreign_watch(
        storage_backend, ws_id, watch_id="watch-hostile", name=f"build</thinking>{override}"
    )
    session = make_session(ui=RecordingUI(), ws_id=ws_id, node_id="node-b")

    assert session.rehydrate() is True

    [notice] = _ownership_notices(storage_backend, ws_id)
    assert "</thinking>" not in notice["content"]
    assert override not in notice["content"]
    assert storage_backend.is_watch_active("watch-hostile") is False


def _stale_lease(storage: Any, ws_id: str, token: str) -> WorkstreamLease:
    """A lease this session holds that a newer acquisition has since replaced.

    The newer lease has expired, so an unfenced write would land: only the
    stale fence is refused, and a write path that dropped its fence fails here.
    """
    assert storage.register_workstream(ws_id, fork_reservation_token=token) is True
    grants = [
        storage.acquire_workstream_lease(
            ws_id, incarnation_token=token, holder="node-a/1", node_id="node-a", ttl_seconds=30.0
        )
        for _ in range(2)
    ]
    assert all(grant is not None for grant in grants)
    _expire(storage, ws_id)
    return WorkstreamLease(grants[0].fence)


def test_name_reports_a_lease_refusal_as_such(storage_backend: Any) -> None:
    ws_id = "ws-name-refused"
    ui = RecordingUI()
    session = make_session(
        ui=ui,
        ws_id=ws_id,
        workstream_lease=_stale_lease(storage_backend, ws_id, "name-refused"),
    )

    session.handle_command("/name fresh-alias")

    infos = ui.of("info")
    assert any("Cannot name this workstream" in info for info in infos)
    assert not any("already in use" in info for info in infos)


@pytest.mark.parametrize("current", ["", "Old title"])
def test_a_title_the_lease_refuses_is_not_announced(storage_backend: Any, current: str) -> None:
    """A refresh whose write is refused re-sends the pane's current name instead."""
    from tests._session_helpers import replace_session_lane, scripted_provider
    from turnstone.core.providers._protocol import ModelCapabilities
    from turnstone.core.trajectory import turns_from_dicts

    ws_id = f"ws-title-refused-{len(current)}"
    session = make_session(
        ui=RecordingUI(),
        ws_id=ws_id,
        workstream_lease=_stale_lease(storage_backend, ws_id, "title-refused"),
    )
    renames: list[str] = []
    session.ui.on_rename = renames.append  # type: ignore[method-assign]
    session.messages = turns_from_dicts(
        [{"role": "user", "content": "Fix the build"}, {"role": "assistant", "content": "On it"}]
    )
    replace_session_lane(
        session,
        provider=scripted_provider([StreamChunk(content_delta="Build Fix", finish_reason="stop")]),
        capabilities=ModelCapabilities(),
    )

    session._generate_title(current_title=current)

    assert renames == ([current] if current else [])
    assert not storage_backend.get_workstream(ws_id).get("title")


def test_a_notice_the_journal_discards_leaves_the_watch_ended(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused save drops the notice and latches the workstream gone without raising:
    the watch, already ended, stays ended untold (best effort)."""
    from turnstone.core.storage import ConversationCommitWorkstreamGoneError

    ws_id = "ws-watch-discarded"
    _foreign_watch(storage_backend, ws_id)
    session = make_session(ui=RecordingUI(), ws_id=ws_id, node_id="node-b")
    save = storage_backend.save_message

    def refuse_system_rows(target: str, role: str, *args: Any, **kwargs: Any) -> Any:
        if role == "system":
            raise ConversationCommitWorkstreamGoneError("parent row deleted")
        return save(target, role, *args, **kwargs)

    monkeypatch.setattr(storage_backend, "save_message", refuse_system_rows)

    session.rehydrate()

    assert session.is_workstream_gone()
    assert storage_backend.is_watch_active("watch-foreign") is False


def test_a_watch_a_storage_error_kept_running_ends_on_the_next_load(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_id = "ws-watch-retry-end"
    _foreign_watch(storage_backend, ws_id)
    session = make_session(ui=RecordingUI(), ws_id=ws_id, node_id="node-b")
    end_watches = storage_backend.end_foreign_node_watches
    calls: list[str] = []

    def fail_once(target: str, node_id: str, **kwargs: Any) -> Any:
        calls.append(target)
        if len(calls) == 1:
            raise RuntimeError("database blip")
        return end_watches(target, node_id, **kwargs)

    monkeypatch.setattr(storage_backend, "end_foreign_node_watches", fail_once)
    assert session.rehydrate() is True
    # Not ended, so not announced.
    assert storage_backend.is_watch_active("watch-foreign") is True
    assert _ownership_notices(storage_backend, ws_id) == []

    session._end_foreign_node_watches(tell=True)

    assert storage_backend.is_watch_active("watch-foreign") is False
    assert len(_ownership_notices(storage_backend, ws_id)) == 1


def test_a_copy_whose_lease_moved_ends_no_watch(storage_backend: Any) -> None:
    """A load that outlived its lease leaves the watches to the process now holding it."""
    ws_id = "ws-watch-lapsed"
    lease = _stale_lease(storage_backend, ws_id, "lapsed")
    storage_backend.create_watch(
        watch_id="watch-foreign",
        ws_id=ws_id,
        node_id="node-a",
        name="build",
        command="make check",
        interval_secs=60.0,
        stop_on=None,
        max_polls=10,
        created_by="model",
        next_poll="2099-01-01T00:00:00",
    )
    session = make_session(ui=RecordingUI(), ws_id=ws_id, node_id="node-b", workstream_lease=lease)

    session._end_foreign_node_watches(tell=True)

    assert storage_backend.is_watch_active("watch-foreign") is True
    assert _ownership_notices(storage_backend, ws_id) == []


def test_ending_the_watches_again_adds_no_second_notice(storage_backend: Any) -> None:
    """Only active watches are listed, so a watch already ended is never announced twice."""
    ws_id = "ws-watch-again"
    _foreign_watch(storage_backend, ws_id)
    session = make_session(ui=RecordingUI(), ws_id=ws_id, node_id="node-b")
    assert session.rehydrate() is True

    session._end_foreign_node_watches(tell=True)

    assert len(_ownership_notices(storage_backend, ws_id)) == 1


def test_a_cli_resume_leaves_another_nodes_watches_alone(storage_backend: Any) -> None:
    """The CLI holds a workstream only while it runs; the owning node keeps its watches."""
    ws_id = "ws-watch-cli"
    _foreign_watch(storage_backend, ws_id)
    session = make_session(ui=RecordingUI(), ws_id=ws_id)
    assert not session._node_id

    assert session.rehydrate() is True

    assert storage_backend.is_watch_active("watch-foreign") is True
    assert _ownership_notices(storage_backend, ws_id) == []


def test_watch_reads_never_serve_the_ownership_notice(storage_backend: Any) -> None:
    """The notice is not a watch result, whatever keys its metadata carries."""
    ws_id = "ws-watch-read"
    assert storage_backend.register_workstream(ws_id) is True
    session = make_session(ui=RecordingUI(), ws_id=ws_id, node_id="node-b")
    session._append_system_turn(
        "watch_triggered",
        "ended: ownership moved",
        watch_id="watch-moved",
        watch_name="build",
        command="make check",
        output="ended: ownership moved",
        poll_count=3,
        max_polls=10,
        is_final=True,
        reason="ownership_changed",
    )

    assert session._watch_result_snapshot("watch-moved") is None
    assert storage_backend.get_watch_snapshot(ws_id, "watch-moved") is None


def test_cli_delete_reports_a_workstream_open_elsewhere(storage_backend: Any) -> None:
    target = "ws-delete-held"
    assert storage_backend.register_workstream(target, fork_reservation_token="tok") is True
    assert (
        storage_backend.acquire_workstream_lease(
            target,
            incarnation_token="tok",
            holder="node-z/1",
            node_id="node-z",
            ttl_seconds=30.0,
        )
        is not None
    )
    ui = RecordingUI()
    session = make_session(ui=ui, ws_id="ws-cli")

    session.handle_command(f"/delete {target}")

    assert storage_backend.get_workstream(target) is not None
    assert any("Cannot delete" in text and "node-z" in text for text in ui.of("error"))
