"""Execution requirements survive lifecycle changes and reject wrong-node loads."""

from __future__ import annotations

import json

import pytest

from tests.test_server_authz import _auth
from tests.test_server_authz import app_client as app_client
from tests.test_session_manager import FakeAdapter
from turnstone.core.node_affinity import NodeAffinityError, parse_required_node_id
from turnstone.core.session_manager import SessionManager
from turnstone.core.storage import get_storage


@pytest.mark.parametrize("value", ["", " a", "a/b", True, 123, [], "a" * 257])
def test_invalid_requirement(value):
    with pytest.raises(ValueError, match="required_node_id"):
        parse_required_node_id(value)


def test_requirement_survives_close_and_rejects_another_manager(db):
    host = SessionManager(FakeAdapter(), storage=db, node_id="host-1", max_active=4)
    other = SessionManager(FakeAdapter(), storage=db, node_id="node-1", max_active=4)
    ws = host.create(user_id="owner", required_node_id="host-1")
    try:
        with pytest.raises(NodeAffinityError):
            other.open(ws.id)
        assert other.list_all() == []
        host.close(ws.id)
        assert db.get_workstream(ws.id)["required_node_id"] == "host-1"
        with pytest.raises(NodeAffinityError):
            other.open(ws.id)
        assert host.open(ws.id) is not None
        assert db.get_workstreams_batch([ws.id])[ws.id]["required_node_id"] == "host-1"
    finally:
        host.close(ws.id)


def test_wrong_node_create_does_not_reserve_or_evict(db):
    manager = SessionManager(FakeAdapter(), storage=db, node_id="node-1", max_active=1)
    current = manager.create(user_id="owner")
    try:
        with pytest.raises(NodeAffinityError):
            manager.create(user_id="owner", ws_id="wrong", required_node_id="host-1")
        assert manager.get(current.id) is current
        assert db.get_workstream("wrong") is None
    finally:
        manager.close(current.id)


@pytest.mark.parametrize("multipart", [False, True])
def test_node_create_and_fork_requirements(app_client, multipart):
    client, manager = app_client
    client.app.state.node_id = manager._node_id = "host-1"
    body = {"required_node_id": "host-1"}
    kwargs = (
        {"files": {"meta": (None, json.dumps(body), "application/json")}}
        if multipart
        else {"json": body}
    )
    response = client.post("/v1/api/workstreams/new", headers=_auth("owner"), **kwargs)
    assert response.status_code == 200, response.text
    source = response.json()["ws_id"]
    storage = get_storage()
    storage.save_message(source, "user", "Saved host history", lease=manager.lease_fence(source))
    manager.close(source)
    response = client.post(
        "/v1/api/workstreams/new", json={"resume_ws": source}, headers=_auth("owner")
    )
    assert response.status_code == 200, response.text
    fork = response.json()["ws_id"]
    assert fork != source
    assert storage.get_workstream(fork)["required_node_id"] == "host-1"
    manager.close(fork)

    client.app.state.node_id = manager._node_id = "node-1"
    response = client.post(
        "/v1/api/workstreams/new", json={"resume_ws": source}, headers=_auth("owner")
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "wrong_execution_node"
    for verb in ("open", "detail"):
        request = client.post if verb == "open" else client.get
        suffix = "/open" if verb == "open" else ""
        response = request(f"/v1/api/workstreams/{source}{suffix}", headers=_auth("owner"))
        assert response.status_code == 409, response.text
    response = client.post(
        "/v1/api/workstreams/new",
        json={"resume_ws": source, "required_node_id": "node-1"},
        headers=_auth("owner"),
    )
    assert response.status_code == 200, response.text
    moved_copy = response.json()["ws_id"]
    assert moved_copy != source
    assert storage.get_workstream(moved_copy)["required_node_id"] == "node-1"
    assert storage.get_workstream(source)["required_node_id"] == "host-1"


@pytest.mark.parametrize("replace_during_load", [False, True])
def test_rehydrate_checks_the_node_before_loading_history(app_client, replace_during_load):
    from unittest.mock import Mock

    from turnstone.core.session import ChatSession

    client, manager = app_client
    client.app.state.node_id = manager._node_id = "host-1"
    source = client.post(
        "/v1/api/workstreams/new", json={"required_node_id": "host-1"}, headers=_auth("owner")
    ).json()["ws_id"]
    session = ChatSession.__new__(ChatSession)
    session._node_id = "host-1" if replace_during_load else "node-1"
    session._ws_id = source
    session._load_message_turns = Mock(return_value=[object()])
    session._read_workstream_config = Mock(return_value={})
    if replace_during_load:
        storage = get_storage()

        def replace_source(_ws_id):
            storage.delete_workstream(source, lease=manager.lease_fence(source))
            storage.register_workstream(source, required_node_id="node-1")
            return [object()]

        session._load_message_turns.side_effect = replace_source
    with pytest.raises(NodeAffinityError):
        session.rehydrate()
    if not replace_during_load:
        session._load_message_turns.assert_not_called()
        session._read_workstream_config.assert_not_called()


@pytest.mark.parametrize("refusal", ["affinity", "lease"])
def test_cli_resume_refuses_requirement_without_leaving_the_tab(monkeypatch, capsys, refusal):
    """The target is bound to another node, or another process holds its owner lease."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from turnstone.cli import _handle_tab_command
    from turnstone.core.storage import WorkstreamLeaseHeldError

    left = SimpleNamespace(id="current", name="current", ui=None, session=None)
    manager = Mock()
    # The current tab stays loaded; the target is not.
    manager.get.side_effect = lambda ws_id: left if ws_id == left.id else None
    manager.loaded.return_value = None
    manager.open.side_effect = (
        NodeAffinityError("host-1", unavailable=True)
        if refusal == "affinity"
        else WorkstreamLeaseHeldError("target", holder_node_id="host-1")
    )
    monkeypatch.setattr("turnstone.core.memory.resolve_workstream", lambda _: "target")

    _handle_tab_command(manager, "/resume target", left, False, {})

    assert "host-1" in capsys.readouterr().out
    manager.switch.assert_not_called()
    manager.close_with_outcome.assert_not_called()


@pytest.mark.parametrize("lifecycle", ["autoclose", "eviction", "restart"])
def test_requirement_survives_lifecycle_and_same_node_restart(db, lifecycle):
    from turnstone.core.session_manager import SessionManager

    manager = SessionManager(FakeAdapter(), storage=db, node_id="host-1", max_active=1)
    ws = manager.create(user_id="owner", required_node_id="host-1")
    try:
        if lifecycle == "autoclose":
            ws.last_active = 0
            assert ws.id in manager.close_idle(max_age_seconds=1)
        elif lifecycle == "eviction":
            manager.create(user_id="owner")
            assert manager.get(ws.id) is None
        else:
            manager.close(ws.id)
        assert db.get_workstream(ws.id)["required_node_id"] == "host-1"
        restarted = SessionManager(FakeAdapter(), storage=db, node_id="host-1", max_active=1)
        wrong = SessionManager(FakeAdapter(), storage=db, node_id="node-1", max_active=1)
        with pytest.raises(NodeAffinityError):
            wrong.open(ws.id)
        try:
            assert restarted.open(ws.id) is not None
        finally:
            restarted.close(ws.id)
    finally:
        for current in manager.list_all():
            manager.close(current.id)


@pytest.mark.parametrize("refusal", ["affinity", "lease"])
def test_server_startup_resume_refusal_does_not_wait(refusal):
    """The target is bound to another node, or another process holds its owner lease."""
    from unittest.mock import Mock

    from turnstone.core.storage import WorkstreamLeaseHeldError
    from turnstone.server import _open_for_startup_resume

    manager = Mock()
    manager.open.side_effect = (
        NodeAffinityError("host-1", unavailable=True)
        if refusal == "affinity"
        else WorkstreamLeaseHeldError("saved", holder_node_id="node-2", retry_after_ms=1500)
    )

    with pytest.raises((NodeAffinityError, WorkstreamLeaseHeldError)):
        _open_for_startup_resume(manager, "saved", node_id="node-1")
    manager.open.assert_called_once_with("saved")


@pytest.mark.parametrize("refusal", ["affinity", "lease", "missing"])
def test_cli_startup_resume_refusal_exits_with_one_line(capsys, refusal):
    from unittest.mock import Mock

    from turnstone.cli import _open_for_cli_resume
    from turnstone.core.storage import WorkstreamLeaseHeldError

    manager = Mock()
    if refusal == "missing":
        manager.open.return_value = None
    else:
        manager.open.side_effect = (
            NodeAffinityError("host-1", unavailable=True)
            if refusal == "affinity"
            else WorkstreamLeaseHeldError("saved", holder_node_id="node-2")
        )

    with pytest.raises(SystemExit) as error:
        _open_for_cli_resume(manager, "saved", "my-alias")

    assert error.value.code == 1
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    if refusal == "missing":
        assert "Cannot resume my-alias: it is not an interactive workstream" in out
    else:
        assert "Cannot resume saved" in out


def test_server_startup_resume_waits_once_for_its_own_previous_process(monkeypatch):
    """A supervised restart finds its crashed predecessor's lease still live: it
    waits that out once instead of exiting into a restart loop."""
    from unittest.mock import Mock

    from turnstone import server
    from turnstone.core.storage import WorkstreamLeaseHeldError

    refusals = [
        WorkstreamLeaseHeldError("saved", holder_node_id="node-1", retry_after_ms=1500),
        WorkstreamLeaseHeldError("saved", holder_node_id="node-2", retry_after_ms=1500),
    ]
    manager = Mock()
    manager.open.side_effect = refusals
    sleeps: list[float] = []
    monkeypatch.setattr(server.time, "sleep", sleeps.append)

    with pytest.raises(WorkstreamLeaseHeldError) as error:
        server._open_for_startup_resume(manager, "saved", node_id="node-1")

    # One wait (the predecessor's remaining lease plus a margin), then the
    # second refusal names another node.
    assert sleeps == [2.5]
    assert error.value.holder_node_id == "node-2"
    assert manager.open.call_count == 2


def test_server_startup_resume_never_waits_without_a_node_id(monkeypatch):
    from unittest.mock import Mock

    from turnstone import server
    from turnstone.core.storage import WorkstreamLeaseHeldError

    manager = Mock()
    manager.open.side_effect = WorkstreamLeaseHeldError(
        "saved", holder_node_id="", retry_after_ms=1500
    )
    sleeps: list[float] = []
    monkeypatch.setattr(server.time, "sleep", sleeps.append)

    with pytest.raises(WorkstreamLeaseHeldError):
        server._open_for_startup_resume(manager, "saved", node_id=None)
    assert sleeps == []
