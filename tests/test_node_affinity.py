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
    storage.save_message(source, "user", "Saved host history")
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


def test_core_resume_checks_node_before_adopting_identity(app_client):
    from turnstone.core.session import ChatSession

    client, manager = app_client
    client.app.state.node_id = manager._node_id = "host-1"
    source = client.post(
        "/v1/api/workstreams/new", json={"required_node_id": "host-1"}, headers=_auth("owner")
    ).json()["ws_id"]
    session = ChatSession.__new__(ChatSession)
    session._node_id, session._ws_id = "node-1", "original"
    session._load_message_turns = lambda _: [object()]
    session._load_workstream_config = lambda _: {}
    with pytest.raises(NodeAffinityError):
        session.resume(source)
    assert session._ws_id == "original"


def test_cli_resume_refuses_requirement_without_exiting_repl(monkeypatch):
    from unittest.mock import Mock

    from turnstone.core.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    session._user_id, session._node_id, session._ws_id = "owner", None, "current"
    session.ui = Mock()
    session._drain_queue_for_identity_swap = lambda: None
    session._load_message_turns = lambda _: [object()]
    session._load_workstream_config = lambda _: {}
    storage = Mock()
    storage.ensure_workstream_incarnation_snapshot.return_value = {
        "required_node_id": "host-1",
        "fork_reservation_token": "token",
    }
    monkeypatch.setattr("turnstone.core.session.current_worker_claim", lambda _: None)
    monkeypatch.setattr("turnstone.core.session.resolve_workstream", lambda _: "target")
    monkeypatch.setattr("turnstone.core.session.get_storage", lambda: storage)
    assert session.handle_command("/resume target", principal_id="owner") is False
    assert session._ws_id == "current"
    session.ui.on_error.assert_called_once()
    assert "host-1" in session.ui.on_error.call_args.args[0]


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


@pytest.mark.parametrize("entrypoint", ["cli.py", "server.py"])
def test_startup_resume_refusal_closes_temporary_session(monkeypatch, entrypoint):
    """Run the startup blocks with real resume, without starting network services."""
    import ast
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock

    from turnstone.core.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    session._node_id, session._ws_id = "node-1", "temporary"
    session._load_message_turns = lambda _: [object()]
    session._load_workstream_config = lambda _: {}
    storage = Mock()
    storage.ensure_workstream_incarnation_snapshot.return_value = {
        "required_node_id": "host-1",
        "fork_reservation_token": "token",
    }
    monkeypatch.setattr("turnstone.core.session.get_storage", lambda: storage)
    monkeypatch.setattr("turnstone.core.memory.resolve_workstream", lambda _: "saved")
    ws = SimpleNamespace(id="temporary", session=session, ui=SimpleNamespace())
    manager = Mock()
    manager.create.return_value = ws
    source = Path(__file__).resolve().parents[1] / "turnstone" / entrypoint
    tree = ast.parse(source.read_text())
    expected_test = "resume_target" if entrypoint == "cli.py" else "args.resume"
    blocks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == expected_test
        and "ws.session.resume(" in ast.unparse(node)
    ]
    assert len(blocks) == 1
    namespace = dict(
        args=SimpleNamespace(resume="saved", skip_permissions=False),
        resume_target="saved",
        ws=ws,
        manager=manager,
        red=lambda value: value,
        sys=sys,
        log=Mock(),
        _get_storage=lambda: storage,
        _watch_restore_owner=lambda *_: "owner",
        _resume_persona_kwargs=lambda _: {},
        WebUI=SimpleNamespace,
        config_store=SimpleNamespace(get=lambda _: False),
    )
    code = compile(ast.Module(body=blocks, type_ignores=[]), str(source), "exec")
    with pytest.raises(SystemExit) as error:
        exec(code, namespace)
    assert error.value.code == 1
    assert session._ws_id == "temporary"
    manager.close.assert_called_once_with("temporary")
