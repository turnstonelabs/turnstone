"""An operator's tool restriction on a coordinator survives a rebuild, and can be lifted.

Runs the console's own coordinator routes (``/restrict``, detail, open, close, events) over real
SQLite with real coordinator sessions: the console session factory builds a ``ChatSession``
(a stub model registry, the settings registry's defaults, a stub coordinator client). A
restart is two managers on one database, as in tests/test_coordinator_events_resume.py. That a
tool is withheld is read off the session itself: its tool dispatch refuses a revoked tool.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _fake_registry
from tests.test_coordinator_events_resume import (
    _PREFIX,
    _WRITER,
    _console_endpoint,
    _Exchange,
    _lane_waiters,
    _make_app,
    _patched,
    _run,
)
from tests.test_coordinator_events_resume import sse_executor as sse_executor
from tests.test_coordinator_events_resume import storage as storage
from tests.test_fork_model_hold import fork_app as fork_app
from turnstone.console.collector import ClusterCollector
from turnstone.console.coordinator_adapter import CoordinatorAdapter
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.console.session_factory import build_console_session_factory
from turnstone.core.session_manager import SessionManager
from turnstone.core.settings_registry import SETTINGS
from turnstone.core.storage import get_storage
from turnstone.core.workstream import WorkstreamKind

if TYPE_CHECKING:
    from collections.abc import Iterator
    from concurrent.futures import ThreadPoolExecutor

    from starlette.applications import Starlette

_SPAWN = "spawn_workstream"
_DELETE = "delete_workstream"


class _Settings:
    """The settings registry's defaults, as a fresh console reads them."""

    def get(self, key: str, default: Any = None) -> Any:
        setting = SETTINGS.get(key)
        return setting.default if setting is not None else default


def _real_mgr(storage: Any) -> SessionManager:
    """A coordinator manager as the console builds one, on the console's own session factory."""
    factory = build_console_session_factory(
        registry=_fake_registry(),
        config_store=_Settings(),  # type: ignore[arg-type]
        node_id="console",
        coord_client_factory=lambda ws_id, user_id: MagicMock(),
    )
    adapter = CoordinatorAdapter(
        collector=MagicMock(),
        ui_factory=lambda ws: ConsoleCoordinatorUI(ws_id=ws.id, user_id=ws.user_id or ""),
        session_factory=factory,
    )
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=None,
        node_id=ClusterCollector.CONSOLE_PSEUDO_NODE_ID,
        event_emitter=adapter,
    )
    adapter.attach(mgr)
    return mgr


def _app(storage: Any, mgr: SessionManager, sse_executor: ThreadPoolExecutor) -> Starlette:
    app = _make_app(storage, mgr, sse_executor)
    app.router.routes.insert(
        0,
        Route(
            _PREFIX + "/{ws_id}/restrict",
            _console_endpoint("POST", "/{ws_id}/restrict"),
            methods=["POST"],
        ),
    )
    return app


@pytest.fixture
def console(storage: Any, sse_executor: ThreadPoolExecutor) -> Iterator[Any]:
    """``open_console(mgr)`` gives a client on the console routes over ``mgr``."""
    clients: list[TestClient] = []

    def open_console(mgr: SessionManager) -> TestClient:
        client = TestClient(_app(storage, mgr, sse_executor))
        client.__enter__()
        clients.append(client)
        return client

    yield open_console
    for client in clients:
        client.__exit__(None, None, None)


def _refuses(session: Any, tool: str) -> bool:
    """Whether the session's tool dispatch refuses ``tool`` as revoked."""
    item = session._prepare_tool({"id": "call-1", "function": {"name": tool, "arguments": "{}"}})
    return "has been revoked" in str(item.get("error") or "")


def _restrict(client: TestClient, ws_id: str, body: dict[str, Any], **headers: str) -> Any:
    return client.post(f"{_PREFIX}/{ws_id}/restrict", json=body, headers={**_WRITER, **headers})


def _revoked_now(mgr: SessionManager, ws_id: str) -> frozenset[str]:
    """The loaded session's revoked set."""
    loaded = mgr.loaded(ws_id)
    assert loaded is not None
    return loaded.session.get_revoked_tools()  # type: ignore[union-attr]


def _saved(storage: Any, ws_id: str) -> Any:
    raw = storage.load_workstream_config(ws_id).get("revoked_tools")
    return None if raw is None else json.loads(raw)


def _audit_details(storage: Any) -> list[dict[str, Any]]:
    """Every ``coordinator.restricted`` row's detail (rows within a second are unordered)."""
    rows = storage.list_audit_events(action="coordinator.restricted")
    return [json.loads(row["detail"]) for row in rows]


def _restricted_coordinator(storage: Any, open_console: Any) -> tuple[SessionManager, str, Any]:
    first = _real_mgr(storage)
    ws_id = first.create(user_id="user-1", name="coord-a").id
    client = open_console(first)
    response = _restrict(client, ws_id, {"revoke": [_SPAWN]})
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "revoked_tools": [_SPAWN], "unknown_tools": []}
    session = first.loaded(ws_id).session  # type: ignore[union-attr]
    assert _refuses(session, _SPAWN) and not _refuses(session, _DELETE)
    assert _saved(storage, ws_id) == [_SPAWN]
    return first, ws_id, client


# ---------------------------------------------------------------------------
# A revoke survives every rebuild
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rebuild", ["graceful-restart", "crash-restart", "close-and-reopen"])
def test_a_restriction_survives_a_rebuild(
    storage: Any, console: Any, sse_executor: ThreadPoolExecutor, rebuild: str
) -> None:
    first, ws_id, client = _restricted_coordinator(storage, console)
    before = first.loaded(ws_id).session  # type: ignore[union-attr]

    if rebuild == "close-and-reopen":
        assert client.post(f"{_PREFIX}/{ws_id}/close", headers=_WRITER).status_code == 200
        assert client.post(f"{_PREFIX}/{ws_id}/open", headers=_WRITER).status_code == 200
        manager = first
    else:
        if rebuild == "graceful-restart":
            first.release_leases()
        manager = _real_mgr(storage)
        assert console(manager).get(f"{_PREFIX}/{ws_id}", headers=_WRITER).status_code == 200

    rebuilt = manager.loaded(ws_id).session  # type: ignore[union-attr]
    assert rebuilt is not before
    assert _refuses(rebuilt, _SPAWN)
    assert not _refuses(rebuilt, _DELETE)
    assert rebuilt.get_revoked_tools() == frozenset({_SPAWN})
    assert manager.close(ws_id)


def test_a_restriction_survives_an_unattended_events_resume(
    storage: Any, console: Any, sse_executor: ThreadPoolExecutor
) -> None:
    first, ws_id, _client = _restricted_coordinator(storage, console)
    first.release_leases()
    second = _real_mgr(storage)
    app = _app(storage, second, sse_executor)
    withheld: list[bool] = []

    async def scenario() -> _Exchange:
        stream = _Exchange(app, "GET", f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
        await stream.wait_until(lambda: len(stream.data()) >= 3)
        resumed = second.loaded(ws_id).session  # type: ignore[union-attr]
        withheld.append(_refuses(resumed, _SPAWN) and not _refuses(resumed, _DELETE))
        await asyncio.to_thread(second.close, ws_id)
        await stream.finish()
        return stream

    stream = _run(scenario())

    assert stream.status == 200
    assert withheld == [True]


# ---------------------------------------------------------------------------
# A restore lifts a restriction, durably
# ---------------------------------------------------------------------------


def test_a_restore_lifts_the_restriction_and_the_lift_survives_a_restart(
    storage: Any, console: Any
) -> None:
    first, ws_id, client = _restricted_coordinator(storage, console)
    assert _restrict(client, ws_id, {"revoke": [_DELETE]}).json()["revoked_tools"] == [
        _DELETE,
        _SPAWN,
    ]

    response = _restrict(client, ws_id, {"restore": [_SPAWN]})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "revoked_tools": [_DELETE], "unknown_tools": []}
    session = first.loaded(ws_id).session  # type: ignore[union-attr]
    assert not _refuses(session, _SPAWN) and _refuses(session, _DELETE)
    assert _saved(storage, ws_id) == [_DELETE]

    first.release_leases()
    second = _real_mgr(storage)
    assert console(second).get(f"{_PREFIX}/{ws_id}", headers=_WRITER).status_code == 200
    rebuilt = second.loaded(ws_id).session  # type: ignore[union-attr]
    assert not _refuses(rebuilt, _SPAWN) and _refuses(rebuilt, _DELETE)
    assert second.close(ws_id)


def test_a_restriction_names_are_trimmed_and_case_variants_refused(
    storage: Any, console: Any
) -> None:
    first, ws_id, client = _restricted_coordinator(storage, console)
    session = first.loaded(ws_id).session  # type: ignore[union-attr]

    trimmed = _restrict(client, ws_id, {"revoke": [f" {_DELETE} "]})
    variant = _restrict(client, ws_id, {"revoke": ["Spawn_Workstream"]})
    spaced = _restrict(client, ws_id, {"revoke": ["spawn workstream"]})
    unknown = _restrict(client, ws_id, {"revoke": ["mcp__later__tool"]})

    assert trimmed.json() == {
        "status": "ok",
        "revoked_tools": [_DELETE, _SPAWN],
        "unknown_tools": [],
    }
    assert _refuses(session, _DELETE)
    assert (variant.status_code, spaced.status_code) == (400, 400)
    assert "did you mean 'spawn_workstream'" in variant.json()["error"]
    # A name no tool matches yet (an MCP server not connected) is applied and reported back.
    assert unknown.status_code == 200
    assert unknown.json()["unknown_tools"] == ["mcp__later__tool"]
    assert _saved(storage, ws_id) == [_DELETE, "mcp__later__tool", _SPAWN]
    assert first.close(ws_id)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"revoke": None, "restore": None},
        {"restore": "spawn_workstream"},
        {"restore": [""]},
        {"restore": ["tool"] * 257},
        {"restore": ["t" * 129]},
        {"restore": ["   "]},
        {"revoke": [_DELETE], "restore": [_SPAWN]},
        {"revoke": [_DELETE], "restore": [_DELETE]},
    ],
    ids=[
        "neither-list",
        "both-null",
        "restore-not-a-list",
        "restore-empty-name",
        "restore-too-many",
        "restore-name-too-long",
        "restore-blank-name",
        "both-lists",
        "a-name-in-both",
    ],
)
def test_a_malformed_restrict_body_is_a_400_and_changes_nothing(
    storage: Any, console: Any, body: dict[str, Any]
) -> None:
    first, ws_id, client = _restricted_coordinator(storage, console)
    audits = len(_audit_details(storage))

    response = _restrict(client, ws_id, body)

    assert response.status_code == 400
    assert _revoked_now(first, ws_id) == frozenset({_SPAWN})
    assert _saved(storage, ws_id) == [_SPAWN]
    assert len(_audit_details(storage)) == audits
    assert first.close(ws_id)


# ---------------------------------------------------------------------------
# A save that fails: each change in the safe direction
# ---------------------------------------------------------------------------


def _failing_restriction_save(real_save: Any) -> Any:
    def save(ws_id: str, config: dict[str, str], **kwargs: Any) -> None:
        if "revoked_tools" in config:
            raise RuntimeError("database is locked")
        real_save(ws_id, config, **kwargs)

    return save


def test_a_revoke_whose_save_fails_is_active_but_says_it_will_not_survive(
    storage: Any, console: Any
) -> None:
    first, ws_id, client = _restricted_coordinator(storage, console)
    session = first.loaded(ws_id).session  # type: ignore[union-attr]

    real_save = storage.save_workstream_config
    with _patched(storage, "save_workstream_config", _failing_restriction_save(real_save)):
        response = _restrict(client, ws_id, {"revoke": [_DELETE]})

    assert response.status_code == 503
    body = response.json()
    assert "active for the running session but was not saved" in body["error"]
    assert body["revoked_tools"] == [_DELETE, _SPAWN]
    assert _refuses(session, _DELETE)
    assert _saved(storage, ws_id) == [_SPAWN]
    assert {
        "src": "coordinator",
        "revoked": [_DELETE],
        "restored": [],
        "revoked_total": [_DELETE, _SPAWN],
        "saved": False,
    } in _audit_details(storage)
    # As the answer says: a rebuild applies what was saved.
    first.release_leases()
    second = _real_mgr(storage)
    assert console(second).get(f"{_PREFIX}/{ws_id}", headers=_WRITER).status_code == 200
    rebuilt = second.loaded(ws_id).session  # type: ignore[union-attr]
    assert _refuses(rebuilt, _SPAWN) and not _refuses(rebuilt, _DELETE)
    assert second.close(ws_id)


def test_a_restore_whose_save_fails_lifts_nothing(storage: Any, console: Any) -> None:
    first, ws_id, client = _restricted_coordinator(storage, console)
    session = first.loaded(ws_id).session  # type: ignore[union-attr]

    real_save = storage.save_workstream_config
    with _patched(storage, "save_workstream_config", _failing_restriction_save(real_save)):
        response = _restrict(client, ws_id, {"restore": [_SPAWN]})

    assert response.status_code == 503
    assert response.json() == {
        "error": "Nothing was lifted: the change could not be saved. Retry shortly.",
        "revoked_tools": [_SPAWN],
        "unknown_tools": [],
    }
    assert _refuses(session, _SPAWN)
    assert _saved(storage, ws_id) == [_SPAWN]
    assert {
        "src": "coordinator",
        "revoked": [],
        "restored": [_SPAWN],
        "revoked_total": [_SPAWN],
        "saved": False,
    } in _audit_details(storage)
    assert first.close(ws_id)


# ---------------------------------------------------------------------------
# A saved restriction that cannot be read never reads as none
# ---------------------------------------------------------------------------


_SAVED_VALUES = {
    "corrupt-value": ("[not json", "the saved tool restriction does not parse"),
    "a-string": ('"spawn_workstream"', "the saved tool restriction is not a list of tool names"),
    "an-object": (
        '{"spawn_workstream": 1}',
        "the saved tool restriction is not a list of tool names",
    ),
    "a-number": ("[1]", "the saved tool restriction is not a list of tool names"),
    "an-empty-name": ('[""]', "the saved tool restriction is not a list of tool names"),
}


@pytest.mark.parametrize("failure", [*_SAVED_VALUES, "read-fails"])
def test_an_unreadable_saved_restriction_refuses_the_open(
    storage: Any, console: Any, failure: str
) -> None:
    first, ws_id, _client = _restricted_coordinator(storage, console)
    first.release_leases()
    if failure in _SAVED_VALUES:
        storage.save_workstream_config(ws_id, {"revoked_tools": _SAVED_VALUES[failure][0]})
    second = _real_mgr(storage)
    client = console(second)

    def failing_read(target: str) -> Any:
        raise RuntimeError("statement timeout")

    if failure == "read-fails":
        with _patched(storage, "load_workstream_config", failing_read):
            response = client.get(f"{_PREFIX}/{ws_id}", headers=_WRITER)
    else:
        response = client.get(f"{_PREFIX}/{ws_id}", headers=_WRITER)

    assert response.status_code == 503
    if failure in _SAVED_VALUES:
        assert response.json() == {"error": _SAVED_VALUES[failure][1]}
    assert second.get(ws_id) is None


# ---------------------------------------------------------------------------
# Authorisation, and forks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Test-Perms": ""},
        {"X-Test-Perms": "", "X-Test-Scopes": "read,write,approve,service"},
    ],
    ids=["no-admin-coordinator", "service-without-admin-coordinator"],
)
@pytest.mark.parametrize("body", [{"revoke": [_DELETE]}, {"restore": [_SPAWN]}])
def test_a_restrict_still_needs_admin_coordinator(
    storage: Any, console: Any, headers: dict[str, str], body: dict[str, Any]
) -> None:
    first, ws_id, client = _restricted_coordinator(storage, console)

    response = client.post(f"{_PREFIX}/{ws_id}/restrict", json=body, headers={**_WRITER, **headers})

    assert response.status_code == 403
    assert _revoked_now(first, ws_id) == frozenset({_SPAWN})
    assert first.close(ws_id)


def test_a_fork_of_a_restricted_coordinator_is_unrestricted(fork_app: Any) -> None:
    """A node's create accepts a coordinator as its fork source; the restriction stays with the
    coordinator it was placed on, in the fork's memory and in its saved config."""
    from turnstone.core.memory import register_workstream, save_message, save_workstream_config

    client, mgr, _registry = fork_app
    source = "c" * 32
    register_workstream(source, user_id="test-user", kind=WorkstreamKind.COORDINATOR)
    save_message(source, "user", "earlier")
    save_workstream_config(
        source,
        {"model_alias": "default", "model": "d-model", "revoked_tools": '["bash", "read_file"]'},
    )

    response = client.post("/v1/api/workstreams/new", json={"resume_ws": source})

    assert response.status_code == 200, response.text
    fork_id = response.json()["ws_id"]
    fork = mgr.get(fork_id)
    assert fork is not None and fork.kind is WorkstreamKind.INTERACTIVE
    assert fork.session.get_revoked_tools() == frozenset()  # type: ignore[union-attr]
    assert "revoked_tools" not in get_storage().load_workstream_config(fork_id)
    assert mgr.close(fork_id)
    reopened = mgr.open(fork_id)
    assert reopened is not None
    assert reopened.session.get_revoked_tools() == frozenset()  # type: ignore[union-attr]
    assert mgr.close(fork_id)
    assert json.loads(get_storage().load_workstream_config(source)["revoked_tools"]) == [
        "bash",
        "read_file",
    ]


@pytest.mark.parametrize("held_at", ["before-the-saved-set-is-read", "after-it-is-read"])
@pytest.mark.parametrize(
    ("body", "expected"),
    [({"revoke": [_SPAWN]}, [_DELETE, _SPAWN]), ({"restore": [_DELETE]}, [])],
    ids=["revoke", "restore"],
)
def test_a_restrict_during_a_rebuild_waits_for_the_saved_set(
    storage: Any, console: Any, held_at: str, body: dict[str, Any], expected: list[str]
) -> None:
    """A rebuilding session is reachable before it has applied the saved restriction. A
    ``/restrict`` in that window waits for the rebuild, so its save starts from the saved set
    and cannot write the session's still-empty one over it."""
    first, ws_id, _client = _restricted_coordinator(storage, console)
    assert _restrict(_client, ws_id, {"revoke": [_DELETE]}).status_code == 200
    assert _restrict(_client, ws_id, {"restore": [_SPAWN]}).status_code == 200
    assert _saved(storage, ws_id) == [_DELETE]
    first.release_leases()
    second = _real_mgr(storage)
    client = console(second)
    entered = threading.Event()
    release = threading.Event()
    answered = threading.Event()
    opened: list[Any] = []
    responses: list[Any] = []
    calls: list[str] = []
    # Both reads are the session's own, inside its rehydrate, with the slot's session already
    # set: its workstream row (before the saved config is read) and its incarnation snapshot,
    # the second one of the open (after the config is read, before the saved set is applied).
    method, hold_on_call = (
        ("get_workstream", 1)
        if held_at == "before-the-saved-set-is-read"
        else ("ensure_workstream_incarnation_snapshot", 2)
    )

    def held(target: str) -> Any:
        if threading.current_thread() is opener:
            calls.append(target)
            if len(calls) == hold_on_call:
                entered.set()
                assert release.wait(timeout=10), "test never released the rehydrate"
        return real(target)

    def restrict() -> None:
        responses.append(_restrict(client, ws_id, body))
        answered.set()

    opener = threading.Thread(target=lambda: opened.append(second.open(ws_id)))
    caller = threading.Thread(target=restrict)
    with _patched(storage, method, held) as real:
        opener.start()
        try:
            assert entered.wait(timeout=10)
            half_built = second.get(ws_id)
            assert half_built is not None and half_built.session is not None
            caller.start()
            # The opener holds the lane, so a restrict that has joined it cannot answer yet;
            # one that skipped the lane answers without ever joining.
            deadline = time.monotonic() + 10
            while _lane_waiters(second, ws_id) < 2 and not answered.is_set():
                assert time.monotonic() < deadline, "the restrict neither waited nor answered"
                time.sleep(0.01)
            assert not answered.is_set(), "the restrict did not wait for the rebuild"
        finally:
            release.set()
            opener.join(timeout=10)
            caller.join(timeout=10)

    assert opened and opened[0] is not None
    assert responses[0].status_code == 200
    assert responses[0].json()["revoked_tools"] == expected
    assert _saved(storage, ws_id) == expected
    session = second.loaded(ws_id).session  # type: ignore[union-attr]
    assert session.get_revoked_tools() == frozenset(expected)
    assert _refuses(session, _DELETE) == (_DELETE in expected)
    assert second.close(ws_id)


def test_a_saved_name_that_became_a_case_variant_can_still_be_lifted(
    storage: Any, console: Any
) -> None:
    """A name revoked while no tool matched it (an MCP server not connected) can later differ
    only in case from a tool the session knows. Restoring that exact entry lifts it."""
    first = _real_mgr(storage)
    ws_id = first.create(user_id="user-1", name="coord-a").id
    first.release_leases()
    storage.save_workstream_config(ws_id, {"revoked_tools": json.dumps(["Spawn_Workstream"])})
    second = _real_mgr(storage)
    client = console(second)
    assert client.get(f"{_PREFIX}/{ws_id}", headers=_WRITER).status_code == 200
    assert _revoked_now(second, ws_id) == frozenset({"Spawn_Workstream"})

    # Still refused on a revoke, and on a restore of a variant that is not in the set.
    assert _restrict(client, ws_id, {"revoke": ["Spawn_Workstream"]}).status_code == 400
    assert _restrict(client, ws_id, {"restore": ["SPAWN_WORKSTREAM"]}).status_code == 400
    lifted = _restrict(client, ws_id, {"restore": ["Spawn_Workstream"]})

    assert lifted.status_code == 200
    assert lifted.json() == {
        "status": "ok",
        "revoked_tools": [],
        "unknown_tools": ["Spawn_Workstream"],
    }
    assert _saved(storage, ws_id) == []
    assert second.close(ws_id)
