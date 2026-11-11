"""A coordinator's event stream resumes a session that was live when its console stopped.

The requests go through the endpoints the console's ``create_app`` mounts for the coordinator
kind, with their production ``SessionEndpointConfig``, over a real SQLite database. A restart is
two managers on one database: the first creates the coordinator and runs it to a live persisted
state, then is dropped, after ``release_leases()`` for a graceful stop and without it for a crash;
the second serves the requests. Streams are read frame by frame through the ASGI app and ended by
closing the workstream, which is how the server ends one.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import inspect
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from tests._coord_test_helpers import MockStorage, _fake_registry, _FakeConfigStore
from tests._resume_stream_contract import resumed_stream_frames
from tests.test_server_authz import _auth
from tests.test_server_authz import app_client as app_client
from turnstone.console.collector import ClusterCollector
from turnstone.console.coordinator_adapter import CoordinatorAdapter
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.core.auth import AuthResult
from turnstone.core.session_manager import CloseOutcome, SessionManager
from turnstone.core.session_routes import SessionEndpointConfig, make_events_handler
from turnstone.core.storage import WorkstreamLeaseHeldError, get_storage
from turnstone.core.workstream import (
    WorkstreamHistoryUnavailableError,
    WorkstreamKind,
    WorkstreamState,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: The event id of the last conversation row the first manager persisted.
_SEED = 7
_WRITER = {"X-Test-User": "user-1"}
_READER = {"X-Test-User": "user-1", "X-Test-Scopes": "read"}
_PREFIX = "/v1/api/workstreams"


# ---------------------------------------------------------------------------
# The console's own coordinator endpoints
# ---------------------------------------------------------------------------


def _walk(routes: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    for route in routes:
        path = prefix + getattr(route, "path", "")
        sub = getattr(route, "routes", None)
        if sub:
            yield from _walk(sub, path)
        else:
            yield path, route


def _endpoints(app: Starlette) -> dict[tuple[str, str], Any]:
    found: dict[tuple[str, str], Any] = {}
    for path, route in _walk(app.routes):
        for method in getattr(route, "methods", None) or ():
            found[(method, path)] = route.endpoint
    return found


@functools.cache
def _console_endpoints() -> dict[tuple[str, str], Any]:
    from turnstone.console.server import create_app

    collector = ClusterCollector(storage=MockStorage(), discovery_interval=999)
    return _endpoints(create_app(collector=collector))


def _console_endpoint(method: str, suffix: str) -> Any:
    return _console_endpoints()[(method, _PREFIX + suffix)]


def _handler_cfg(endpoint: Any) -> SessionEndpointConfig:
    cfg = inspect.getclosurevars(endpoint).nonlocals["cfg"]
    assert isinstance(cfg, SessionEndpointConfig)
    return cfg


class _Auth:
    """Inject an ``AuthResult`` from ``X-Test-User`` / ``X-Test-Scopes`` / ``X-Test-Perms``.

    A plain ASGI middleware, so a streamed response and its disconnect pass straight through.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        user = headers.get("x-test-user", "")
        if scope["type"] == "http" and user:
            scopes = headers.get("x-test-scopes", "read,write,approve")
            perms = headers.get("x-test-perms", "admin.coordinator")
            scope.setdefault("state", {})["auth_result"] = AuthResult(
                user_id=user,
                scopes=frozenset(s for s in scopes.split(",") if s),
                token_source="test",
                permissions=frozenset(p for p in perms.split(",") if p),
            )
        await self._app(scope, receive, send)


@pytest.fixture
def storage(tmp_path: Any) -> Iterator[Any]:
    # The coordinator UI seeds its event counter through the registry singleton.
    from turnstone.core.storage import init_storage, reset_storage

    reset_storage()
    backend = init_storage("sqlite", path=str(tmp_path / "coord.db"), run_migrations=False)
    yield backend
    reset_storage()


@pytest.fixture
def sse_executor() -> Iterator[ThreadPoolExecutor]:
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="test-coord-sse")
    yield executor
    executor.shutdown(wait=True)


class _Factory:
    """The coordinator session factory: records each build, or raises ``fail``."""

    def __init__(self) -> None:
        self.builds: list[str] = []
        self.fail: BaseException | None = None

    def __call__(self, ui: Any, model_alias: Any = None, ws_id: Any = None, **_kw: Any) -> Any:
        self.builds.append(ws_id)
        if self.fail is not None:
            raise self.fail
        session = MagicMock()
        session.model = "gpt-test"
        session.model_alias = "default"
        session._last_usage = None
        session._acting_user_id = ""
        session._user_id = ""
        session.project_name_for_principal = lambda principal: ""
        return session


def _build_mgr(storage: Any, factory: _Factory, *, max_active: int | None = None) -> SessionManager:
    """A coordinator manager as the console builds one: uncapped, on the console pseudo node."""
    adapter = CoordinatorAdapter(
        collector=MagicMock(),
        ui_factory=lambda ws: ConsoleCoordinatorUI(ws_id=ws.id, user_id=ws.user_id or ""),
        session_factory=factory,
    )
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=max_active,
        node_id=ClusterCollector.CONSOLE_PSEUDO_NODE_ID,
        event_emitter=adapter,
    )
    adapter.attach(mgr)
    return mgr


def _must_open(mgr: SessionManager, ws_id: str) -> Any:
    """Open ``ws_id`` on ``mgr`` and return its slot; the open must succeed."""
    ws = mgr.open(ws_id)
    assert ws is not None
    return ws


def _must_close(mgr: SessionManager, ws_id: str) -> None:
    """Close ``ws_id`` on ``mgr``; the close must succeed."""
    closed = mgr.close(ws_id)
    assert closed


def _rehydrated_count(mgr: SessionManager) -> int:
    """How often the adapter announced a rehydrated coordinator to the dashboard."""
    collector: Any = mgr._adapter._collector  # type: ignore[attr-defined]
    return int(collector.emit_console_ws_created.call_count)


def _closed_count(mgr: SessionManager) -> int:
    """How often the adapter announced a closed coordinator to the dashboard."""
    collector: Any = mgr._adapter._collector  # type: ignore[attr-defined]
    return int(collector.emit_console_ws_closed.call_count)


def _live_coordinator(
    storage: Any, *, graceful: bool, state: WorkstreamState = WorkstreamState.RUNNING
) -> str:
    """Create a coordinator in a first manager, run it to ``state``, then stop that manager."""
    first = _build_mgr(storage, _Factory())
    ws_id = first.create(user_id="user-1", name="coord-a").id
    storage.save_message(
        ws_id, "user", "plan the rollout", event_id=_SEED, lease=first.lease_fence(ws_id)
    )
    first.set_state(ws_id, state)
    assert storage.get_workstream(ws_id)["state"] == state.value
    if graceful:
        first.release_leases()
    return ws_id


def _make_app(
    storage: Any,
    mgr: SessionManager,
    sse_executor: ThreadPoolExecutor,
    *,
    events: Any = None,
) -> Starlette:
    """The console's coordinator events, detail, open and close endpoints over ``mgr``."""
    app = Starlette(
        routes=[
            Route(
                _PREFIX + "/{ws_id}/events",
                events or _console_endpoint("GET", "/{ws_id}/events"),
                methods=["GET"],
            ),
            Route(
                _PREFIX + "/{ws_id}/open",
                _console_endpoint("POST", "/{ws_id}/open"),
                methods=["POST"],
            ),
            Route(
                _PREFIX + "/{ws_id}/close",
                _console_endpoint("POST", "/{ws_id}/close"),
                methods=["POST"],
            ),
            Route(_PREFIX + "/{ws_id}", _console_endpoint("GET", "/{ws_id}"), methods=["GET"]),
        ],
    )
    app.state.coord_mgr = mgr
    app.state.coord_adapter = mgr._adapter
    app.state.config_store = _FakeConfigStore({"coordinator.model_alias": "default"})
    app.state.coord_registry = _fake_registry()
    app.state.coord_registry_error = ""
    app.state.auth_storage = storage
    app.state.jwt_secret = "x" * 64
    app.state.coord_sse_executor = sse_executor
    app.add_middleware(_Auth)
    return app


# ---------------------------------------------------------------------------
# One request through the ASGI app, read while it streams
# ---------------------------------------------------------------------------


class _Exchange:
    """One request driven through the ASGI app; its body is readable while it streams."""

    def __init__(self, app: Any, method: str, url: str, headers: dict[str, str]) -> None:
        path, _, query = url.partition("?")
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver")]
            + [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        self._app = app
        self.status: int | None = None
        self.body = bytearray()
        self.ended = False
        self._changed = asyncio.Event()
        self._gone = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        requested = False

        async def receive() -> dict[str, Any]:
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self._gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
            elif message["type"] == "http.response.body":
                self.body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    self.ended = True
            self._changed.set()

        try:
            await self._app(self._scope, receive, send)
        finally:
            self.ended = True
            self._changed.set()

    async def wait_until(self, predicate: Callable[[], bool], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            self._changed.clear()
            if predicate():
                return
            if self.ended:
                raise AssertionError(
                    f"ended before the condition held: {self.status} {self.body!r}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out: {self.status} {self.body!r}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)

    async def finish(self, timeout: float = 10.0) -> None:
        """Wait for the response to end; a request still running then is disconnected."""
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        finally:
            if not self._task.done():
                self._gone.set()
                await asyncio.wait_for(self._task, timeout)

    def json(self) -> Any:
        return json.loads(bytes(self.body))

    def frames(self) -> list[dict[str, Any]]:
        """Each SSE event's fields, ``data`` decoded; comments (pings) left out."""
        out: list[dict[str, Any]] = []
        text = bytes(self.body).decode().replace("\r\n", "\n")
        for block in text.split("\n\n"):
            fields: dict[str, Any] = {}
            for line in block.split("\n"):
                if not line or line.startswith(":"):
                    continue
                name, _, value = line.partition(":")
                fields[name] = value[1:] if value.startswith(" ") else value
            if fields:
                if "data" in fields:
                    fields["data"] = json.loads(fields["data"])
                out.append(fields)
        return out

    def data(self) -> list[dict[str, Any]]:
        return [frame["data"] for frame in self.frames() if "data" in frame]


async def _answer(app: Any, url: str, headers: dict[str, str], method: str = "GET") -> _Exchange:
    """A request expected to be refused: it ends, or a stream it opened is disconnected."""
    exchange = _Exchange(app, method, url, headers)
    await exchange.wait_until(lambda: exchange.status is not None)
    if not exchange.ended and exchange.status is not None and exchange.status < 300:
        exchange._gone.set()
    await exchange.finish()
    return exchange


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@contextlib.contextmanager
def _patched(obj: Any, name: str, replacement: Any) -> Iterator[Any]:
    """Replace ``obj.name`` for the block, yielding the original; ``finally`` puts it back.

    An instance override is restored from the held reference; a class attribute is uncovered
    again by dropping the override.
    """
    own = vars(obj)
    held = own.get(name)
    had_own = name in own
    original = getattr(obj, name)
    setattr(obj, name, replacement)
    try:
        yield original
    finally:
        if had_own:
            setattr(obj, name, held)
        else:
            delattr(obj, name)


def _lane_waiters(mgr: SessionManager, ws_id: str) -> int:
    """How many callers hold or wait on ``ws_id``'s per-id lane."""
    with mgr._lock:
        entry = mgr._open_locks.get(ws_id)
    return entry[1] if entry is not None else 0


async def _until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.01)


def _bootstrap_tail(ws_id: str) -> list[dict[str, Any]]:
    """The replay of a freshly resumed coordinator after its envelope frame."""
    return resumed_stream_frames(ws_id, lost_count=0, earliest_available_id=0)[1:]


def _assert_nothing_loaded(mgr: SessionManager, factory: _Factory, ws_id: str) -> None:
    assert mgr.get(ws_id) is None
    assert factory.builds == []
    assert mgr.lease_fence(ws_id) is None
    assert mgr._lease_keeper.tracked() == []
    assert _rehydrated_count(mgr) == 0


class _HeldFactory(_Factory):
    """Holds the first build until ``release``; ``fail_builds`` of them then raise."""

    def __init__(self, *, fail_builds: int = 0) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_builds = fail_builds

    def __call__(self, ui: Any, model_alias: Any = None, ws_id: Any = None, **kw: Any) -> Any:
        if not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(timeout=10), "test never released the build"
        if self.fail_builds:
            self.fail_builds -= 1
            self.builds.append(ws_id)
            raise RuntimeError("model client construction failed")
        return super().__call__(ui, model_alias, ws_id, **kw)


async def _until_streaming(stream: _Exchange) -> None:
    """Until the stream answered with its bootstrap, or ended with a refusal."""
    await stream.wait_until(lambda: stream.ended or len(stream.data()) >= 3)


# ---------------------------------------------------------------------------
# Resume, and what the stream says after it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("graceful", "cursor", "lost"),
    # Ephemeral events of the old process carried ids past the last persisted row (the usual
    # case); a cursor behind it lost the rows in between.
    [(True, _SEED + 3, 0), (False, _SEED - 2, 2)],
    ids=["graceful-cursor-past-seed", "crash-cursor-behind-seed"],
)
def test_a_stale_cursor_resumes_a_coordinator_live_at_restart(
    storage: Any, sse_executor: ThreadPoolExecutor, graceful: bool, cursor: int, lost: int
) -> None:
    ws_id = _live_coordinator(storage, graceful=graceful)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    async def scenario() -> _Exchange:
        url = f"{_PREFIX}/{ws_id}/events?last_event_id={cursor}&user_turn=1&tool_turn=1"
        stream = _Exchange(app, "GET", url, _WRITER)
        await stream.wait_until(lambda: len(stream.data()) >= 3)
        assert second.loaded(ws_id) is not None
        await asyncio.to_thread(second.close, ws_id)
        await stream.finish()
        return stream

    stream = _run(scenario())

    assert stream.status == 200
    assert set(stream.frames()[0]) == {"retry"}
    # The contract the pane test feeds to the real panes (tests/_resume_stream_contract.py).
    assert stream.frames()[1:] == resumed_stream_frames(
        ws_id, lost_count=lost, earliest_available_id=_SEED + 1
    )
    assert factory.builds == [ws_id]
    assert _rehydrated_count(second) == 1


def test_a_resume_without_a_cursor_bootstraps_like_a_fresh_tab(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    async def scenario() -> _Exchange:
        stream = _Exchange(app, "GET", f"{_PREFIX}/{ws_id}/events", _WRITER)
        await stream.wait_until(lambda: len(stream.data()) >= 3)
        await asyncio.to_thread(second.close, ws_id)
        await stream.finish()
        return stream

    stream = _run(scenario())

    assert stream.status == 200
    assert stream.frames()[1:] == [
        {"data": {"type": "clear_ui", "ws_id": ws_id, "reason": "tokenless_history_bootstrap"}},
        *_bootstrap_tail(ws_id),
    ]
    assert factory.builds == [ws_id]


def test_a_failing_post_load_hook_never_blocks_the_resumed_stream(
    storage: Any, sse_executor: ThreadPoolExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    calls: list[str] = []

    def failing_hook(request: Any, ws: Any) -> None:
        calls.append(ws.id)
        raise RuntimeError("post-load hook bug")

    cfg = dataclasses.replace(
        _handler_cfg(_console_endpoint("GET", "/{ws_id}/events")), open_post_load=failing_hook
    )
    app = _make_app(storage, second, sse_executor, events=make_events_handler(cfg))

    async def scenario() -> _Exchange:
        stream = _Exchange(app, "GET", f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
        await stream.wait_until(lambda: len(stream.data()) >= 3)
        await asyncio.to_thread(second.close, ws_id)
        await stream.finish()
        return stream

    with caplog.at_level(logging.DEBUG):
        stream = _run(scenario())

    assert stream.status == 200
    assert stream.frames()[2:] == _bootstrap_tail(ws_id)
    assert calls == [ws_id]
    # A positional template, so message plus args (see the refusal test).
    rendered = [f"{record.getMessage()} {record.args!r}" for record in caplog.records]
    assert any("ws.events.post_load_failed" in line and ws_id[:8] in line for line in rendered)


# ---------------------------------------------------------------------------
# What stays 404, and builds nothing
# ---------------------------------------------------------------------------


def test_a_closed_coordinator_answers_404_and_builds_nothing(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    closer = _build_mgr(storage, _Factory())
    _must_open(closer, ws_id)
    _must_close(closer, ws_id)
    assert storage.get_workstream(ws_id)["state"] == "closed"
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    async def scenario() -> tuple[_Exchange, _Exchange]:
        events = await _answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id={_SEED}", _WRITER)
        close = await _answer(app, f"{_PREFIX}/{ws_id}/close", _WRITER, method="POST")
        return events, close

    events, close = _run(scenario())

    assert (events.status, close.status) == (404, 404)
    assert events.json() == close.json() == {"error": "coordinator not found"}
    _assert_nothing_loaded(second, factory, ws_id)
    assert storage.get_workstream(ws_id)["state"] == "closed"


@pytest.mark.parametrize("closed", [False, True], ids=["live-row", "closed-row"])
def test_a_reader_gets_the_plain_404_and_nothing_loads(
    storage: Any, sse_executor: ThreadPoolExecutor, closed: bool
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    if closed:
        storage.update_workstream_state(ws_id, "closed")
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    refused = _run(_answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id={_SEED}", _READER))

    assert refused.status == 404
    assert refused.json() == {"error": "coordinator not found"}
    _assert_nothing_loaded(second, factory, ws_id)


def _seed_masked_row(storage: Any, case: str) -> tuple[str, dict[str, str]]:
    ws_id = "c" * 32
    if case == "interactive":
        storage.register_workstream(ws_id, kind="interactive", user_id="user-1", state="idle")
    elif case == "creating":
        storage.register_workstream(ws_id, kind="coordinator", user_id="user-1", state="creating")
    else:
        storage.create_project("proj-secret", "Secret", "alice")
        storage.register_workstream(
            ws_id, kind="coordinator", user_id="alice", project_id="proj-secret", state="idle"
        )
        return ws_id, {"X-Test-User": "stranger"}
    return ws_id, _WRITER


@pytest.mark.parametrize("case", ["interactive", "creating", "private-project"])
def test_rows_the_tenant_check_masks_answer_404_and_load_nothing(
    storage: Any, sse_executor: ThreadPoolExecutor, case: str
) -> None:
    ws_id, headers = _seed_masked_row(storage, case)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    refused = _run(_answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=3", headers))

    assert refused.status == 404
    assert refused.json() == {"error": "coordinator not found"}
    _assert_nothing_loaded(second, factory, ws_id)


def test_a_project_member_resumes_a_private_coordinator(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    storage.create_project("proj-secret", "Secret", "alice")
    storage.add_project_member("proj-secret", "member-bob")
    ws_id = "c" * 32
    storage.register_workstream(
        ws_id, kind="coordinator", user_id="alice", project_id="proj-secret", state="attention"
    )
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)
    owners: list[str] = []

    async def scenario() -> _Exchange:
        stream = _Exchange(app, "GET", f"{_PREFIX}/{ws_id}/events", {"X-Test-User": "member-bob"})
        await stream.wait_until(lambda: len(stream.data()) >= 3)
        resumed = second.loaded(ws_id)
        assert resumed is not None
        owners.append(resumed.user_id)
        await asyncio.to_thread(second.close, ws_id)
        await stream.finish()
        return stream

    stream = _run(scenario())

    assert stream.status == 200
    # The session runs as the row's owner, whoever's tab dialled back.
    assert owners == ["alice"]
    assert factory.builds == [ws_id]


# ---------------------------------------------------------------------------
# A close of a coordinator that is not loaded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("graceful", [True, False], ids=["graceful", "crash"])
def test_closing_a_coordinator_live_at_restart_writes_closed_without_loading_it(
    storage: Any, sse_executor: ThreadPoolExecutor, graceful: bool
) -> None:
    ws_id = _live_coordinator(storage, graceful=graceful)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    async def scenario() -> list[_Exchange]:
        close = await _answer(app, f"{_PREFIX}/{ws_id}/close", _WRITER, method="POST")
        again = await _answer(app, f"{_PREFIX}/{ws_id}/close", _WRITER, method="POST")
        redial = await _answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
        return [close, again, redial]

    close, again, redial = _run(scenario())

    assert close.status == 200
    assert close.json() == {"status": "ok"}
    # The row closed and its audit written, with no session built and no ``ws_created``.
    assert storage.get_workstream(ws_id)["state"] == "closed"
    assert len(storage.list_audit_events(action="coordinator.close")) == 1
    assert factory.builds == []
    assert _rehydrated_count(second) == 0
    assert _closed_count(second) == 1
    assert (again.status, redial.status) == (404, 404)
    assert second.get(ws_id) is None
    assert second._lease_keeper.tracked() == []
    assert storage.get_workstream(ws_id)["lease_node_id"] is None


def test_an_unbuildable_coordinator_still_closes(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    factory.fail = RuntimeError("model client construction failed")
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    close = _run(_answer(app, f"{_PREFIX}/{ws_id}/close", _WRITER, method="POST"))

    assert close.status == 200
    assert storage.get_workstream(ws_id)["state"] == "closed"
    assert factory.builds == []


def test_a_close_meeting_a_failing_resume_build_still_closes_the_row(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _HeldFactory(fail_builds=1)
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    async def scenario() -> list[_Exchange]:
        url = f"{_PREFIX}/{ws_id}/events?last_event_id=9"
        resume = _Exchange(app, "GET", url, _WRITER)
        try:
            await _until(factory.entered.is_set)
            # The resume's half-built slot is installed; the close waits on the lane.
            assert second.get(ws_id) is not None and second.loaded(ws_id) is None
            close = _Exchange(app, "POST", f"{_PREFIX}/{ws_id}/close", _WRITER)
            await _until(lambda: _lane_waiters(second, ws_id) >= 2)
        finally:
            factory.release.set()
        await resume.finish()
        await close.finish()
        redial = await _answer(app, url, _WRITER)
        return [resume, close, redial]

    resume, close, redial = _run(scenario())

    assert resume.status is not None and resume.status >= 400
    assert close.status == 200
    assert storage.get_workstream(ws_id)["state"] == "closed"
    assert redial.status == 404
    assert second.get(ws_id) is None
    assert factory.builds == [ws_id]


def test_a_close_that_cannot_be_recorded_is_a_503_and_changes_nothing(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    def failing_update(target: str, state: str, **kwargs: Any) -> None:
        if target == ws_id and state == "closed":
            raise RuntimeError("database is locked")
        real_update(target, state, **kwargs)

    with _patched(storage, "update_workstream_state", failing_update) as real_update:
        refused = _run(_answer(app, f"{_PREFIX}/{ws_id}/close", _WRITER, method="POST"))

    assert refused.status == 503
    assert refused.json() == {"error": "the close could not be recorded; try again shortly"}
    assert storage.get_workstream(ws_id)["state"] == "running"
    assert _closed_count(second) == 0
    assert second._lease_keeper.tracked() == []
    # The write's lease was released: a retry with storage healthy closes it.
    retried = _run(_answer(app, f"{_PREFIX}/{ws_id}/close", _WRITER, method="POST"))
    assert retried.status == 200
    assert storage.get_workstream(ws_id)["state"] == "closed"


def test_a_close_leaves_a_retained_failed_delete_alone(storage: Any) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    second = _build_mgr(storage, _Factory())
    ws = second.open(ws_id)
    assert ws is not None
    # A failed delete whose journal is unresolved keeps its slot, and its lease, for the retry.
    second._retain_failed_persisted_delete_tombstone(ws, was_unadvertised=False)
    grants: list[str] = []

    def recording_acquire(target: str, **kwargs: Any) -> Any:
        grants.append(target)
        return real_acquire(target, **kwargs)

    with _patched(storage, "acquire_workstream_lease", recording_acquire) as real_acquire:
        outcome = second.close_persisted_with_outcome(ws_id)

    assert outcome is CloseOutcome.NOT_FOUND
    assert grants == []
    assert storage.get_workstream(ws_id)["state"] == "running"


def test_a_writer_dialling_during_a_close_gets_the_close_outcome(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    loaded = second.open(ws_id)
    assert loaded is not None
    app = _make_app(storage, second, sse_executor)
    entered = threading.Event()
    release = threading.Event()

    def held_prepare() -> bool:
        entered.set()
        assert release.wait(timeout=10), "test never released the close"
        return True

    loaded.session.prepare_soft_close = held_prepare

    async def scenario() -> list[_Exchange]:
        close = _Exchange(app, "POST", f"{_PREFIX}/{ws_id}/close", _WRITER)
        try:
            await _until(entered.is_set)
            stream = _Exchange(app, "GET", f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
            await _until(lambda: _lane_waiters(second, ws_id) >= 2)
        finally:
            release.set()
        await close.finish()
        await stream.wait_until(lambda: stream.status is not None)
        await stream.finish()
        return [close, stream]

    close, stream = _run(scenario())

    assert close.status == 200
    assert stream.status == 404
    assert storage.get_workstream(ws_id)["state"] == "closed"
    assert factory.builds == [ws_id]


# ---------------------------------------------------------------------------
# Races decided inside the open's own lane
# ---------------------------------------------------------------------------


def test_a_close_racing_the_resume_is_not_undone_by_it(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    _must_open(second, ws_id)
    app = _make_app(storage, second, sse_executor)
    entered = threading.Event()
    release = threading.Event()

    def held_update(target: str, state: str, **kwargs: Any) -> None:
        if target == ws_id and state == "closed":
            entered.set()
            assert release.wait(timeout=10), "test never released the closed write"
        real_update(target, state, **kwargs)

    async def scenario() -> _Exchange:
        closing = asyncio.create_task(asyncio.to_thread(second.close, ws_id))
        try:
            await _until(entered.is_set)
            # The close unloaded the slot and holds the lane through its closed write.
            assert second.get(ws_id) is None
            exchange = _Exchange(app, "GET", f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
            await _until(lambda: _lane_waiters(second, ws_id) >= 2)
        finally:
            release.set()
            assert await closing
        await exchange.wait_until(lambda: exchange.status is not None)
        await exchange.finish()
        return exchange

    with _patched(storage, "update_workstream_state", held_update) as real_update:
        refused = _run(scenario())

    assert refused.status == 404
    assert factory.builds == [ws_id]  # the open before the close, and nothing since
    assert second.get(ws_id) is None
    assert storage.get_workstream(ws_id)["state"] == "closed"


def test_the_reaper_closing_the_row_mid_resume_retires_the_slot_and_releases_the_lease(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)
    reaped: list[str] = []

    def snapshot_then_reap(target: str) -> Any:
        row = real_snapshot(target)
        if not reaped:
            # The orphan reaper takes no lane: it closes the still-unleased row between the
            # open's snapshot and its lease grant.
            reaped.extend(
                storage.bulk_close_stale_orphans(
                    WorkstreamKind.COORDINATOR, "9999-12-31T23:59:59", []
                )
            )
        return row

    with _patched(
        storage, "ensure_workstream_incarnation_snapshot", snapshot_then_reap
    ) as real_snapshot:
        refused = _run(_answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER))

    assert reaped == [ws_id]
    assert refused.status == 404
    assert refused.json() == {"error": "coordinator not found"}
    assert factory.builds == [ws_id]  # built, then refused on the re-read
    assert second.get(ws_id) is None
    assert second.lease_fence(ws_id) is None
    assert second._lease_keeper.tracked() == []
    assert _rehydrated_count(second) == 0
    row = storage.get_workstream(ws_id)
    assert row["state"] == "closed"
    assert row["lease_node_id"] is None


def test_the_lease_retry_carries_the_closed_refusal(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)
    replaced: list[bool] = []

    def snapshot_then_replace(target: str) -> Any:
        row = real_snapshot(target)
        if not replaced:
            replaced.append(True)
            # Another incarnation takes the id, and it is closed: the lease bound to the first
            # snapshot's token is refused, and the open retries from the successor's snapshot.
            storage.delete_workstream(target)
            storage.register_workstream(
                target,
                kind="coordinator",
                user_id="user-1",
                state="closed",
                fork_reservation_token="token-successor",
            )
        return row

    with _patched(
        storage, "ensure_workstream_incarnation_snapshot", snapshot_then_replace
    ) as real_snapshot:
        refused = _run(_answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER))

    assert replaced == [True]
    assert refused.status == 404
    _assert_nothing_loaded(second, factory, ws_id)


def test_the_witness_retry_carries_the_closed_refusal(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)
    reads: list[str] = []

    def snapshot(target: str) -> Any:
        reads.append(target)
        if len(reads) == 2:
            # The post-build re-read finds a live successor: the open retires its build and
            # retries from a fresh snapshot.
            fence = second.lease_fence(target)
            assert fence is not None
            storage.release_workstream_lease(fence)
            storage.delete_workstream(target)
            storage.register_workstream(
                target,
                kind="coordinator",
                user_id="user-1",
                state="idle",
                fork_reservation_token="token-successor",
            )
        elif len(reads) == 3:
            # By the retry's snapshot the successor was closed.
            storage.update_workstream_state(target, "closed")
        return real_snapshot(target)

    with _patched(storage, "ensure_workstream_incarnation_snapshot", snapshot) as real_snapshot:
        refused = _run(_answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER))

    assert len(reads) == 3
    assert refused.status == 404
    assert factory.builds == [ws_id]  # the first build only, retired at the re-read
    assert second.get(ws_id) is None
    assert second._lease_keeper.tracked() == []


def test_two_streams_on_one_unloaded_coordinator_build_once_and_post_load_once(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    post_loads: list[str] = []
    cfg = dataclasses.replace(
        _handler_cfg(_console_endpoint("GET", "/{ws_id}/events")),
        open_post_load=lambda request, ws: post_loads.append(ws.id),
    )
    app = _make_app(storage, second, sse_executor, events=make_events_handler(cfg))
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()

    def held_snapshot(target: str) -> Any:
        if not snapshot_entered.is_set():
            snapshot_entered.set()
            assert release_snapshot.wait(timeout=10), "test never released the snapshot"
        return real_snapshot(target)

    async def scenario() -> list[_Exchange]:
        url = f"{_PREFIX}/{ws_id}/events?last_event_id=9"
        first = _Exchange(app, "GET", url, _WRITER)
        try:
            await _until(snapshot_entered.is_set)
            other = _Exchange(app, "GET", url, {"X-Test-User": "user-2"})
            # Both requests are in the open: one holds the lane, the other waits on it.
            await _until(lambda: _lane_waiters(second, ws_id) >= 2)
        finally:
            release_snapshot.set()
        for stream in (first, other):
            await stream.wait_until(lambda s=stream: len(s.data()) >= 3)
        await asyncio.to_thread(second.close, ws_id)
        for stream in (first, other):
            await stream.finish()
        return [first, other]

    with _patched(
        storage, "ensure_workstream_incarnation_snapshot", held_snapshot
    ) as real_snapshot:
        streams = _run(scenario())

    assert [stream.status for stream in streams] == [200, 200]
    for stream in streams:
        assert stream.data()[0]["type"] == "replay_truncated"
        assert stream.frames()[2:] == _bootstrap_tail(ws_id)
    assert factory.builds == [ws_id]
    assert post_loads == [ws_id]
    assert _rehydrated_count(second) == 1


@pytest.mark.parametrize(
    ("fail_builds", "statuses"),
    # The build succeeds; it fails once (the writer who waited runs its own); it fails every
    # time (one refusal per tab, never a stream that ends like a reconnect).
    [(0, [200, 200]), (1, [500, 200]), (2, [500, 500])],
    ids=["build-succeeds", "build-fails-once", "build-keeps-failing"],
)
def test_a_second_writer_waits_for_the_open_in_flight_and_gets_its_outcome(
    storage: Any, sse_executor: ThreadPoolExecutor, fail_builds: int, statuses: list[int]
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _HeldFactory(fail_builds=fail_builds)
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)
    seen_while_building: list[int | None] = []

    async def scenario() -> list[_Exchange]:
        url = f"{_PREFIX}/{ws_id}/events?last_event_id=9"
        first = _Exchange(app, "GET", url, _WRITER)
        try:
            await _until(factory.entered.is_set)
            # The first build is in flight: its half-built slot is installed.
            assert second.get(ws_id) is not None and second.loaded(ws_id) is None
            other = _Exchange(app, "GET", url, {"X-Test-User": "user-2"})
            await _until(lambda: _lane_waiters(second, ws_id) >= 2)
            seen_while_building.append(other.status)
        finally:
            factory.release.set()
        for stream in (first, other):
            await _until_streaming(stream)
        if second.get(ws_id) is not None:
            await asyncio.to_thread(second.close, ws_id)
        for stream in (first, other):
            await stream.finish()
        return [first, other]

    streams = _run(scenario())

    assert seen_while_building == [None]
    assert [stream.status for stream in streams] == statuses
    for stream in streams:
        if stream.status == 200:
            assert stream.frames()[2:] == _bootstrap_tail(ws_id)
    assert len(factory.builds) == (1 if fail_builds == 0 else 2)


class _RehydrateHeld(_Factory):
    """Each session's rehydrate holds until ``release``, then fails as an unreadable history."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, ui: Any, model_alias: Any = None, ws_id: Any = None, **kw: Any) -> Any:
        session = super().__call__(ui, model_alias, ws_id, **kw)

        def rehydrate() -> bool:
            self.entered.set()
            assert self.release.wait(timeout=10), "test never released the rehydrate"
            raise WorkstreamHistoryUnavailableError(ws_id)

        session.rehydrate.side_effect = rehydrate
        return session


def test_a_second_writer_waits_through_the_rehydrate_and_gets_its_own_refusal(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _RehydrateHeld()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)
    seen_while_rehydrating: list[int | None] = []

    async def scenario() -> list[_Exchange]:
        url = f"{_PREFIX}/{ws_id}/events?last_event_id=9"
        first = _Exchange(app, "GET", url, _WRITER)
        try:
            await _until(factory.entered.is_set)
            # The slot carries its session now, so the manager vouches for it mid-rehydrate.
            assert second.loaded(ws_id) is not None
            other = _Exchange(app, "GET", url, {"X-Test-User": "user-2"})
            await _until(lambda: _lane_waiters(second, ws_id) >= 2)
            seen_while_rehydrating.append(other.status)
        finally:
            factory.release.set()
        for stream in (first, other):
            await stream.finish()
        return [first, other]

    streams = _run(scenario())

    assert seen_while_rehydrating == [None]
    assert [stream.status for stream in streams] == [503, 503]
    assert all(not stream.data() for stream in streams)
    assert len(factory.builds) == 2
    assert second.get(ws_id) is None


def test_a_reader_still_streams_a_slot_being_built(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _HeldFactory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)
    reader_frames: list[list[dict[str, Any]]] = []

    async def scenario() -> _Exchange:
        url = f"{_PREFIX}/{ws_id}/events?last_event_id=9"
        writer = _Exchange(app, "GET", url, _WRITER)
        try:
            await _until(factory.entered.is_set)
            reader = _Exchange(app, "GET", url, _READER)
            await reader.wait_until(lambda: len(reader.data()) >= 2)
            reader_frames.append(reader.data())
        finally:
            factory.release.set()
        await writer.wait_until(lambda: len(writer.data()) >= 3)
        await asyncio.to_thread(second.close, ws_id)
        await writer.finish()
        await reader.finish()
        return reader

    reader = _run(scenario())

    assert reader.status == 200
    # Today's attach for a caller who may not resume: no session yet, so no ``connected``.
    assert [frame["type"] for frame in reader_frames[0]] == ["replay_truncated", "state_change"]


# ---------------------------------------------------------------------------
# Unloads this process did not record durably
# ---------------------------------------------------------------------------


def test_a_close_whose_closed_write_failed_is_not_resumed_and_a_retried_close_records_it(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    _must_open(second, ws_id)
    app = _make_app(storage, second, sse_executor)

    def failing_update(target: str, state: str, **kwargs: Any) -> None:
        if target == ws_id and state == "closed":
            raise RuntimeError("database is locked")
        real_update(target, state, **kwargs)

    with _patched(storage, "update_workstream_state", failing_update) as real_update:
        _must_close(second, ws_id)
    assert storage.get_workstream(ws_id)["state"] == "running"

    async def scenario() -> list[_Exchange]:
        redial = await _answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
        close = await _answer(app, f"{_PREFIX}/{ws_id}/close", _WRITER, method="POST")
        return [redial, close]

    redial, close = _run(scenario())

    # The mark keeps the pane's redial from undoing the close; a retried close records it.
    assert redial.status == 404
    assert close.status == 200
    assert storage.get_workstream(ws_id)["state"] == "closed"
    assert ws_id not in second._unrecorded_unloads
    assert factory.builds == [ws_id]


def test_an_explicit_open_after_a_failed_closed_write_streams_again(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    """The mark refuses only the unattended resume. A deliberate open loads the coordinator and
    clears it, and a writer's stream is then served; a mark on an id that is loaded (a load
    path that forgot to clear it) blocks no stream either."""
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    _must_open(second, ws_id)
    app = _make_app(storage, second, sse_executor)

    def failing_update(target: str, state: str, **kwargs: Any) -> None:
        if target == ws_id and state == "closed":
            raise RuntimeError("database is locked")
        real_update(target, state, **kwargs)

    with _patched(storage, "update_workstream_state", failing_update) as real_update:
        _must_close(second, ws_id)
    assert ws_id in second._unrecorded_unloads

    async def scenario() -> list[int | None]:
        detail = _Exchange(app, "GET", f"{_PREFIX}/{ws_id}", _WRITER)
        await detail.finish()
        cleared = ws_id not in second._unrecorded_unloads
        stream = await _answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
        second._unrecorded_unloads.add(ws_id)
        marked = await _answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
        await asyncio.to_thread(second.close, ws_id)
        return [detail.status, cleared, stream.status, marked.status]

    assert _run(scenario()) == [200, True, 200, 200]
    assert factory.builds == [ws_id, ws_id]


def test_a_failed_delete_is_not_resumed(storage: Any, sse_executor: ThreadPoolExecutor) -> None:
    ws_id = _live_coordinator(storage, graceful=True, state=WorkstreamState.IDLE)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    _must_open(second, ws_id)
    app = _make_app(storage, second, sse_executor)

    def locked_delete(*, lease: Any = None) -> bool:
        raise RuntimeError("database is locked")

    with pytest.raises(RuntimeError, match="database is locked"):
        second.delete_persisted(ws_id, delete_fn=locked_delete, name="coord-a")
    # The journal was complete, so the slot was retired with nothing durable written.
    assert second.get(ws_id) is None
    assert storage.get_workstream(ws_id)["state"] == "idle"

    redial = _run(_answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER))

    assert redial.status == 404
    assert factory.builds == [ws_id]
    assert second.get(ws_id) is None
    assert second.delete_persisted(
        ws_id,
        delete_fn=lambda *, lease=None: storage.delete_workstream(ws_id, lease=lease),
        name="coord-a",
    )
    assert storage.get_workstream(ws_id) is None
    assert ws_id not in second._unrecorded_unloads


# ---------------------------------------------------------------------------
# Refusals: every one non-2xx, mapped as the open and detail handlers map them
# ---------------------------------------------------------------------------


def _refuse_lease(ws: str, **_kwargs: Any) -> Any:
    # SQLite takes a live lease over by contract; model another console's live hold.
    raise WorkstreamLeaseHeldError(ws, holder_node_id="console", retry_after_ms=900)


@pytest.mark.parametrize(
    ("route", "case", "status"),
    [
        ("events", "lease-held", 409),
        ("close", "lease-held", 409),
        ("events", "internal-error", 500),
    ],
    ids=["events-lease-held", "close-lease-held", "events-internal-error"],
)
def test_a_refused_resume_is_never_2xx_and_loads_nothing(
    storage: Any,
    sse_executor: ThreadPoolExecutor,
    caplog: pytest.LogCaptureFixture,
    route: str,
    case: str,
    status: int,
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)
    patches = contextlib.ExitStack()
    if case == "lease-held":
        patches.enter_context(_patched(storage, "acquire_workstream_lease", _refuse_lease))
    else:
        factory.fail = RuntimeError("boom")
    url, method = (
        (f"{_PREFIX}/{ws_id}/events?last_event_id=9", "GET")
        if route == "events"
        else (f"{_PREFIX}/{ws_id}/close", "POST")
    )

    with patches, caplog.at_level(logging.WARNING):
        refused = _run(_answer(app, url, _WRITER, method=method))

    assert refused.status == status
    body = refused.json()
    if case == "lease-held":
        assert body["code"] == "workstream_lease_held"
    else:
        event = "ws.events.resume_failed"
        assert body["error"].startswith("failed to resume coordinator (internal error).")
        correlation_id = body["error"].rsplit("correlation_id=", 1)[1]
        # The shared failure log is a positional template, so the id is not a keyed field:
        # structlog's test renderer appends ``positional_args=(...)`` and leaves ``args``
        # empty, a stdlib configuration formats them in. Message plus args holds either way.
        rendered = [f"{record.getMessage()} {record.args!r}" for record in caplog.records]
        assert any(event in line and correlation_id in line for line in rendered)
    assert second.get(ws_id) is None
    assert second.lease_fence(ws_id) is None
    assert second._lease_keeper.tracked() == []
    assert _rehydrated_count(second) == 0
    assert storage.get_workstream(ws_id)["state"] == "running"


# ---------------------------------------------------------------------------
# The open and detail handlers still reopen a closed row; only the resume refuses it
# ---------------------------------------------------------------------------


def test_open_and_detail_still_reopen_a_closed_coordinator(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    ws_id = _live_coordinator(storage, graceful=True)
    storage.update_workstream_state(ws_id, "closed")
    factory = _Factory()
    second = _build_mgr(storage, factory)
    app = _make_app(storage, second, sse_executor)

    async def scenario() -> list[int | None]:
        statuses: list[int | None] = []
        events = await _answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
        statuses.append(events.status)
        for method, url in (("GET", f"{_PREFIX}/{ws_id}"), ("POST", f"{_PREFIX}/{ws_id}/open")):
            exchange = _Exchange(app, method, url, _WRITER)
            await exchange.finish()
            statuses.append(exchange.status)
            await asyncio.to_thread(second.close, ws_id)
        return statuses

    statuses = _run(scenario())

    assert statuses == [404, 200, 200]
    assert factory.builds == [ws_id, ws_id]


@pytest.mark.parametrize("verb", ["detail", "open"])
def test_a_coordinator_reopened_from_closed_resumes_after_a_restart(
    storage: Any, sse_executor: ThreadPoolExecutor, verb: str
) -> None:
    """Reopening a closed coordinator records its live state at once. A console that stops
    before the coordinator is used again leaves a live row, so an open pane's redial resumes
    it instead of finding the ``closed`` its last close wrote."""
    ws_id = _live_coordinator(storage, graceful=True)
    storage.update_workstream_state(ws_id, "closed")
    second = _build_mgr(storage, _Factory())
    reopening = _make_app(storage, second, sse_executor)
    method, url = (
        ("GET", f"{_PREFIX}/{ws_id}") if verb == "detail" else ("POST", f"{_PREFIX}/{ws_id}/open")
    )

    async def reopen() -> int | None:
        exchange = _Exchange(reopening, method, url, _WRITER)
        await exchange.finish()
        return exchange.status

    assert _run(reopen()) == 200
    assert storage.get_workstream(ws_id)["state"] == "idle"
    # The console stops with the coordinator loaded and never used.
    second.release_leases()
    factory = _Factory()
    third = _build_mgr(storage, factory)
    app = _make_app(storage, third, sse_executor)

    async def redial() -> int | None:
        stream = _Exchange(app, "GET", f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
        await stream.wait_until(lambda: stream.status is not None)
        status = stream.status
        await asyncio.to_thread(third.close, ws_id)
        await stream.finish()
        return status

    assert _run(redial()) == 200
    assert factory.builds == [ws_id]
    assert storage.get_workstream(ws_id)["state"] == "closed"


def test_the_reopen_records_the_state_on_the_state_lane(storage: Any) -> None:
    """The slot is reachable during its open, so the reopen's state write takes the lane every
    state write takes and reads the state inside it: a transition admitted beside it cannot be
    overwritten by an older ``idle``."""
    ws_id = _live_coordinator(storage, graceful=True)
    storage.update_workstream_state(ws_id, "closed")
    second = _build_mgr(storage, _Factory())
    writes: list[tuple[str, bool]] = []

    def recording(ws: Any, state: WorkstreamState) -> None:
        writes.append((state.value, ws._state_tail_lock.locked()))
        real_persist(ws, state)

    def moved_during_the_open(ws: Any) -> bool:
        # A send that reached the half-built slot: its transition is admitted, its own
        # write not yet made.
        with ws._lock:
            second._apply_live_state(ws, WorkstreamState.ATTENTION, "")
        return bool(real_retain(ws))

    with (
        _patched(second, "_persist_state", recording) as real_persist,
        _patched(second, "_retain_current_state_tail", moved_during_the_open) as real_retain,
    ):
        _must_open(second, ws_id)

    assert writes == [("attention", True)]
    assert storage.get_workstream(ws_id)["state"] == "attention"
    _must_close(second, ws_id)


# ---------------------------------------------------------------------------
# The interactive kind is unchanged
# ---------------------------------------------------------------------------


def test_interactive_events_on_an_evicted_row_stays_404(app_client: Any) -> None:
    client, manager = app_client
    evicted = client.post("/v1/api/workstreams/new", json={}, headers=_auth("owner")).json()
    manager._max_active = 1
    kept = client.post("/v1/api/workstreams/new", json={}, headers=_auth("owner")).json()
    # Eviction writes nothing durable: the row stays live and unleased.
    assert manager.get(evicted["ws_id"]) is None
    row = get_storage().get_workstream(evicted["ws_id"])
    assert row["state"] not in {"closed", "deleted"}
    assert row["lease_node_id"] is None

    url = f"{_PREFIX}/{evicted['ws_id']}/events?last_event_id=3"
    response = _run(_answer(client.app, url, _auth("owner")))

    assert response.status == 404
    assert manager.get(evicted["ws_id"]) is None
    assert manager.get(kept["ws_id"]) is not None


# ---------------------------------------------------------------------------
# A console stop announces no close (the shell closes a coordinator pane on ws_closed)
# ---------------------------------------------------------------------------


def test_a_console_stop_announces_no_close(storage: Any) -> None:
    mgr = _build_mgr(storage, _Factory())
    mgr.create(user_id="user-1", name="coord-a")
    # The console lifespan's teardown order: the adapter, then the manager's leases.
    mgr._adapter.shutdown()  # type: ignore[attr-defined]
    mgr.release_leases()
    assert _closed_count(mgr) == 0


# ---------------------------------------------------------------------------
# Production wiring, and the uncapped-manager precondition
# ---------------------------------------------------------------------------


def test_only_the_console_coordinator_mount_resumes_and_only_on_an_uncapped_manager(
    storage: Any, sse_executor: ThreadPoolExecutor
) -> None:
    from turnstone.server import create_app as create_node_app

    console_cfg = _handler_cfg(_console_endpoint("GET", "/{ws_id}/events"))
    assert console_cfg.events_resume_live is True
    assert console_cfg.list_kind is WorkstreamKind.COORDINATOR
    console_close = inspect.getclosurevars(_console_endpoint("POST", "/{ws_id}/close"))
    assert console_close.nonlocals["close_unloaded_rows"] is True

    manager = MagicMock()
    manager.list_all.return_value = []
    manager.max_active = 10
    node_app = create_node_app(
        workstreams=manager,
        global_queue=queue.Queue(),
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
        jwt_secret="x" * 64,
    )
    node_cfg = _handler_cfg(_endpoints(node_app)[("GET", _PREFIX + "/{ws_id}/events")])
    assert node_cfg.list_kind is WorkstreamKind.INTERACTIVE
    # The only pin of the node's opt-out: the interactive tests run on a capped manager, which
    # refuses a resume whatever the flag says.
    assert node_cfg.events_resume_live is False
    node_close = inspect.getclosurevars(_endpoints(node_app)[("POST", _PREFIX + "/{ws_id}/close")])
    assert node_close.nonlocals["close_unloaded_rows"] is False

    # A capped coordinator manager never resumes, flag or not: an eviction writes nothing
    # durable, so a resume would undo it. Its close needs no session, so it still closes.
    ws_id = _live_coordinator(storage, graceful=True)
    factory = _Factory()
    capped = _build_mgr(storage, factory, max_active=8)
    app = _make_app(storage, capped, sse_executor)

    async def scenario() -> list[_Exchange]:
        redial = await _answer(app, f"{_PREFIX}/{ws_id}/events?last_event_id=9", _WRITER)
        close = await _answer(app, f"{_PREFIX}/{ws_id}/close", _WRITER, method="POST")
        return [redial, close]

    redial, close = _run(scenario())

    assert (redial.status, close.status) == (404, 200)
    _assert_nothing_loaded(capped, factory, ws_id)
    assert storage.get_workstream(ws_id)["state"] == "closed"
