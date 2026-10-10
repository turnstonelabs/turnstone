"""Shutdown of accepted primes and revocations over real local HTTP (#1147)."""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import contextlib
import gc
import json
import queue
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from aiohttp import web
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from tests._proc_helpers import poll_until
from tests.conftest import make_mcp_token_cipher
from tests.test_mcp_oauth_handlers import _InjectAuthMiddleware
from turnstone.core import mcp_client, mcp_oauth
from turnstone.core.mcp_client import (
    MCPClientManager,
    MCPShutdownError,
    PoolEntryState,
    StaticServerState,
)
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.oauth.context import oauth_context
from turnstone.core.oauth.oidc import OIDCConfig
from turnstone.core.oauth.runtime import shutdown_oauth_runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

_KEY = ("user-1", "pool-srv")


async def _until(predicate: Any) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


class _Upstream:
    """An AS and MCP endpoint with event-controlled response boundaries.

    With a *session_id*, the MCP endpoint assigns that session at initialize, so a client ends
    it with a DELETE when its transport closes, and the ``delete`` phase holds that request;
    *hold_session_end* holds it in every phase. The ``initialize-stream`` phase sends the
    session's headers and then never the result, so the client holds a session id but no
    session.
    """

    def __init__(
        self, phase: str, *, session_id: str | None = None, hold_session_end: bool = False
    ) -> None:
        self.phase = phase
        self.session_id = session_id
        self.hold_session_end = hold_session_end
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[str] = []
        self.revoked = False

    async def __aenter__(self) -> _Upstream:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app, shutdown_timeout=0.2)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.origin = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.release.set()
        await self.runner.cleanup()

    async def gate(self, phase: str) -> None:
        if phase == self.phase:
            self.entered.set()
            await self.release.wait()

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(request.path)
        if request.path == "/.well-known/oauth-authorization-server":
            await self.gate("discovery")
            return web.json_response(
                {
                    "issuer": self.origin,
                    "authorization_endpoint": self.origin + "/authorize",
                    "token_endpoint": self.origin + "/token",
                    "revocation_endpoint": self.origin + "/revoke",
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                }
            )
        if request.path == "/revoke":
            form = await request.post()
            assert form["token"] == "refresh-shutdown-sentinel"
            await self.gate("revoke")
            self.revoked = True
            return web.Response(status=200)
        if request.path == "/token":
            form = await request.post()
            assert form["refresh_token"] == "refresh-shutdown-sentinel"
            return web.json_response(
                {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
            )
        if request.path != "/mcp":
            return web.Response(status=404)
        if request.method == "DELETE" and self.session_id is not None:
            if self.hold_session_end:
                await self.release.wait()
            else:
                await self.gate("delete")
            return web.Response(status=200)
        if request.method != "POST":
            return web.Response(status=405)
        body = await request.json()
        if "id" not in body:
            return web.Response(status=202)
        method = body["method"]
        if method == "initialize" and self.phase == "initialize-stream":
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "mcp-session-id": self.session_id or "",
                },
            )
            await response.prepare(request)
            self.entered.set()
            await self.release.wait()
            return response
        await self.gate(method)
        headers: dict[str, str] = {}
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "shutdown-test", "version": "1"},
            }
            if self.session_id is not None:
                headers["mcp-session-id"] = self.session_id
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            assert method == "tools/list"
            result = {"tools": [{"name": "example", "inputSchema": {"type": "object"}}]}
        return web.json_response(
            {"jsonrpc": "2.0", "id": body["id"], "result": result}, headers=headers
        )


async def _host(
    storage: Any, origin: str, *, expires_at: str = "2099-12-31T00:00:00"
) -> tuple[Starlette, MCPTokenStore]:
    storage.create_mcp_server(
        server_id="srv-pool",
        name=_KEY[1],
        transport="streamable-http",
        url=origin + "/mcp",
        auth_type="oauth_user",
        oauth_client_id="test-client",
        oauth_authorization_server_url=origin,
        oauth_audience=origin + "/mcp",
    )
    tokens = MCPTokenStore(storage, make_mcp_token_cipher(), node_id="test")
    tokens.create_user_token(
        *_KEY,
        access_token="access-shutdown-sentinel",
        refresh_token="refresh-shutdown-sentinel",
        expires_at=expires_at,
        scopes="openid",
        as_issuer=origin,
        audience=origin + "/mcp",
    )
    app = Starlette(
        routes=[
            Route(
                "/connections/{server_name}",
                mcp_oauth.handle_mcp_oauth_revoke_connection,
                methods=["DELETE"],
            )
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    oauth_context(app.state).token_store = tokens
    await mcp_oauth.initialize_mcp_oauth_state(app.state)
    return app, tokens


async def _disconnect(app: Starlette) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.delete("/connections/pool-srv")


async def _manager(storage: Any, app: Starlette) -> MCPClientManager:
    manager = MCPClientManager({})
    manager._user_token_sweep_s = 0
    manager._static_health_check_s = 0
    manager._oauth_user_server_names.add(_KEY[1])
    manager.set_storage(storage)
    manager.set_app_state(app.state)
    await asyncio.to_thread(manager.start)
    return manager


@pytest.mark.parametrize(
    ("phase", "entrypoint"),
    [
        ("lookup", "session"),
        ("initialize", "session"),
        ("initialize", "consent"),
        ("tools/list", "session"),
        ("tools/list", "consent"),
    ],
)
def test_shutdown_drains_real_prime(
    phase: str,
    entrypoint: str,
    tmp_path: Any,
    sqlite_backend_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = sqlite_backend_factory(str(tmp_path / "prime.db"))
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})

    async def run() -> None:
        async with _Upstream(phase) as upstream:
            app, tokens = await _host(storage, upstream.origin)
            manager = await _manager(storage, app)
            loop, thread = manager._loop, manager._thread
            assert loop is not None and thread is not None
            entered, release = threading.Event(), threading.Event()
            workers: list[threading.Thread] = []
            read = tokens.get_user_token

            def held_read(*args: Any, **kwargs: Any) -> Any:
                workers.append(threading.current_thread())
                entered.set()
                assert release.wait(10)
                return read(*args, **kwargs)

            if phase == "lookup":
                monkeypatch.setattr(tokens, "get_user_token", held_read)
            try:
                if entrypoint == "session":
                    manager.prime_user_pools(_KEY[0])
                else:
                    manager.schedule_prime_user_server(
                        user_id=_KEY[0],
                        server_name=_KEY[1],
                        access_token="access-shutdown-sentinel",
                        server_row=storage.get_mcp_server_by_name(_KEY[1]),
                    )
                if phase == "lookup":
                    await _until(entered.is_set)
                else:
                    await asyncio.wait_for(upstream.entered.wait(), 5)

                async def held_locks() -> list[asyncio.Lock]:
                    if phase == "lookup":
                        return [app.state.mcp_oauth_coordination.locks[_KEY]]
                    return [manager._user_pool_entries[_KEY].open_lock]

                locks = await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(held_locks(), loop)
                )
                assert all(lock.locked() for lock in locks)
                await asyncio.to_thread(manager.shutdown)
                await asyncio.to_thread(shutdown_oauth_runtime, app.state)
                assert loop.is_closed() and not thread.is_alive()
                assert not asyncio.all_tasks(loop)
                assert not any(lock.locked() for lock in locks)
                assert not manager._priming_keys
                assert not manager._user_tools
                assert read(*_KEY) is not None
            finally:
                release.set()
                upstream.release.set()
                for worker in workers:
                    await asyncio.to_thread(worker.join, 5)
                    assert not worker.is_alive()
                await asyncio.to_thread(manager.shutdown)
                await asyncio.to_thread(shutdown_oauth_runtime, app.state)
                await mcp_oauth.close_mcp_oauth_state(app.state)

    asyncio.run(run())


def test_prime_shutdown_preserves_started_refresh_write(
    tmp_path: Any,
    sqlite_backend_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = sqlite_backend_factory(str(tmp_path / "refresh.db"))
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})

    async def run() -> None:
        async with _Upstream("unused") as upstream:
            app, tokens = await _host(storage, upstream.origin, expires_at="2000-01-01T00:00:00")
            write = tokens.update_user_token_after_refresh

            def held_write(*args: Any, **kwargs: Any) -> Any:
                entered.set()
                assert release.wait(10)
                return write(*args, **kwargs)

            monkeypatch.setattr(tokens, "update_user_token_after_refresh", held_write)
            manager = await _manager(storage, app)
            loop = manager._loop
            assert loop is not None
            stopping = None
            try:
                manager.prime_user_pools(_KEY[0])
                await _until(entered.is_set)
                context = oauth_context(app.state)
                runtime = context.runtime
                assert runtime is not None
                client = context.http_client
                assert client is not None
                lock = context.coordination.locks[_KEY]
                await asyncio.to_thread(manager.shutdown)
                assert loop.is_closed() and not asyncio.all_tasks(loop)
                assert lock.locked()
                stopping = asyncio.create_task(asyncio.to_thread(shutdown_oauth_runtime, app.state))
                await _until(lambda: not runtime._accepting)
                assert not stopping.done() and not client.is_closed
                release.set()
                await asyncio.wait_for(stopping, 5)
                assert not lock.locked() and client.is_closed
                token = tokens.get_user_token(*_KEY)
                assert token is not None and token["refresh_token"] == "new-refresh"
                assert runtime._thread is not None and not runtime._thread.is_alive()
                assert not manager._user_tools
            finally:
                release.set()
                if stopping is not None:
                    await asyncio.wait_for(stopping, 5)
                await asyncio.to_thread(manager.shutdown)
                await asyncio.to_thread(shutdown_oauth_runtime, app.state)
                await mcp_oauth.close_mcp_oauth_state(app.state)

    asyncio.run(run())


@pytest.mark.parametrize("entrypoint", ["session", "consent"])
def test_grouped_prime_failure_keeps_attribution_and_sibling_ownership(
    entrypoint: str,
    tmp_path: Any,
    sqlite_backend_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = sqlite_backend_factory(str(tmp_path / "group.db"))
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})

    async def run() -> None:
        async with _Upstream("unused") as upstream:
            app, tokens = await _host(storage, upstream.origin)
            storage.create_mcp_server(
                server_id="srv-sibling",
                name="sibling",
                transport="streamable-http",
                url=upstream.origin + "/mcp",
                auth_type="oauth_user",
            )
            tokens.create_user_token(
                _KEY[0],
                "sibling",
                access_token="sibling-access",
                refresh_token=None,
                expires_at="2099-12-31T00:00:00",
                scopes="openid",
                as_issuer=upstream.origin,
                audience=upstream.origin + "/mcp",
            )
            manager = await _manager(storage, app)
            manager._oauth_user_server_names.add("sibling")
            loop = manager._loop
            assert loop is not None
            sibling_started, release = asyncio.Event(), asyncio.Event()
            failed, sibling_cancelled = threading.Event(), threading.Event()

            async def prime(key: tuple[str, str], *_: Any) -> int:
                if key != _KEY:
                    sibling_started.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        sibling_cancelled.set()
                        raise
                    return 0
                if entrypoint == "session":
                    await sibling_started.wait()
                failed.set()
                raise BaseExceptionGroup(
                    "transport failed", [asyncio.CancelledError(), RuntimeError("closed")]
                )

            monkeypatch.setattr(manager, "_prime_user_server", prime)
            try:
                if entrypoint == "session":
                    manager.prime_user_pools(_KEY[0])
                    await _until(
                        lambda: (
                            failed.is_set()
                            and (
                                _KEY in manager._pool_discovery_error
                                or not manager._background_tasks
                            )
                        )
                    )
                    status = manager.get_server_status(_KEY[1], user_id=_KEY[0])
                    # The failure the group carries, not the cancellation beside it.
                    assert status["discovery_error"] == "RuntimeError: closed"
                    assert manager._background_tasks
                else:
                    manager.schedule_prime_user_server(
                        user_id=_KEY[0],
                        server_name=_KEY[1],
                        access_token="access-shutdown-sentinel",
                        server_row=storage.get_mcp_server_by_name(_KEY[1]),
                    )
                    await _until(lambda: failed.is_set() and not manager._background_tasks)
                    assert any(
                        all(
                            detail in record.getMessage()
                            for detail in ("mcp pool prime failed", *_KEY)
                        )
                        for record in caplog.records
                    )
                await asyncio.to_thread(manager.shutdown)
                if entrypoint == "session":
                    assert sibling_cancelled.is_set()
                assert loop.is_closed() and not asyncio.all_tasks(loop)
                assert not manager._priming_keys
            finally:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(release.set)
                await asyncio.to_thread(manager.shutdown)
                await asyncio.to_thread(shutdown_oauth_runtime, app.state)
                await mcp_oauth.close_mcp_oauth_state(app.state)

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["grace", "cancel"])
def test_cancelled_revoke_drain_still_closes_client_and_resets_state(
    phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if phase == "cancel":
        monkeypatch.setattr(mcp_oauth, "_REVOKE_UPSTREAM_DRAIN_TIMEOUT", 0)

    async def run() -> None:
        state = SimpleNamespace()
        await mcp_oauth.initialize_mcp_oauth_state(state)
        client = state.mcp_oauth_http_client
        coordination = state.mcp_oauth_coordination
        state.mcp_oauth_dcr_locks["test"] = asyncio.Lock()
        state.mcp_oauth_metadata_cache["test"] = object()
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def revoke() -> None:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
                await release.wait()

        task = asyncio.create_task(revoke())
        revocations = mcp_oauth._upstream_revocations(state)
        revocations.tasks.add(task)
        task.add_done_callback(revocations.tasks.discard)
        closing = None
        try:
            await entered.wait()
            closing = asyncio.create_task(mcp_oauth.close_mcp_oauth_state(state))
            if phase == "cancel":
                await cancelled.wait()
            else:
                await _until(lambda: revocations.closing)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert client.is_closed and state.mcp_oauth_http_client is None
            assert state.mcp_oauth_coordination is not coordination
            assert not state.mcp_oauth_dcr_locks
            assert not state.mcp_oauth_metadata_cache
        finally:
            if closing is not None:
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
            release.set()
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await mcp_oauth.close_mcp_oauth_state(state)

    asyncio.run(run())


def test_node_shutdown_allows_revocation_to_finish_during_manager_drain(
    tmp_path: Any, sqlite_backend_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from turnstone import server

    storage = sqlite_backend_factory(str(tmp_path / "node.db"))
    monkeypatch.setattr(server.WebUI, "_workstream_mgr", None)
    monkeypatch.setattr(
        "turnstone.core.mcp_crypto.initialize_mcp_crypto_state", lambda *_, **__: None
    )

    async def run() -> None:
        async with _Upstream("revoke") as upstream:
            app, _ = await _host(storage, upstream.origin)
            await mcp_oauth.close_mcp_oauth_state(app.state)
            oauth_context(app.state).oidc_config = OIDCConfig(enabled=False)
            app.state.global_queue = queue.Queue()
            app.state.global_listeners = []
            app.state.global_listeners_lock = threading.Lock()
            app.state.global_event_buffer = deque()
            app.state.global_event_id_holder = [0]
            app.state.workstreams = MagicMock()
            app.state.idle_timeout = 0
            app.state.rate_limiter = None
            app.state.watch_runner = None
            app.state.registry = None
            entered, finished = threading.Event(), threading.Event()
            observed: list[bool] = []

            def shutdown() -> None:
                entered.set()
                observed.append(finished.wait(5))

            app.state.mcp_client = SimpleNamespace(shutdown=shutdown)
            async with server._lifespan(app):
                client = app.state.mcp_oauth_http_client
                response = await _disconnect(app)
                assert response.status_code == 204
                await asyncio.wait_for(upstream.entered.wait(), 5)
                task = next(iter(mcp_oauth._upstream_revocations(app.state).tasks))

                async def finish_revoke() -> None:
                    await _until(entered.is_set)
                    upstream.release.set()
                    await task
                    finished.set()

                progress = asyncio.create_task(finish_revoke())
            await asyncio.wait_for(progress, 5)
            assert observed == [True]
            assert upstream.revoked and client.is_closed
            # Shutdown hands every workstream lease back.
            app.state.workstreams.release_leases.assert_called_once()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["discovery", "revoke"])
@pytest.mark.parametrize("finish", [True, False])
def test_close_drains_real_revocation_before_client(
    phase: str,
    finish: bool,
    tmp_path: Any,
    sqlite_backend_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = sqlite_backend_factory(str(tmp_path / "revoke.db"))
    caplog.set_level("INFO", logger="turnstone.core.mcp_oauth")
    if not finish:
        monkeypatch.setattr(mcp_oauth, "_REVOKE_UPSTREAM_DRAIN_TIMEOUT", 0.01)

    async def run() -> None:
        async with _Upstream(phase) as upstream:
            app, tokens = await _host(storage, upstream.origin)
            client = app.state.mcp_oauth_http_client
            close = client.aclose
            observed: list[bool] = []
            revocations = mcp_oauth._upstream_revocations(app.state)

            async def observed_close() -> None:
                observed.append(task.done())
                await close()

            try:
                response = await _disconnect(app)
                assert response.status_code == 204
                assert tokens.get_user_token(*_KEY) is None
                await asyncio.wait_for(upstream.entered.wait(), 5)
                task = next(iter(revocations.tasks))
                monkeypatch.setattr(client, "aclose", observed_close)
                closing = asyncio.create_task(mcp_oauth.close_mcp_oauth_state(app.state))
                await _until(lambda: revocations.closing)
                if finish:
                    assert not client.is_closed
                    upstream.release.set()
                await asyncio.wait_for(closing, 2)
                assert task.done() and task.cancelled() is not finish
                assert client.is_closed and observed == [True]
                assert not revocations.tasks
                assert tokens.get_user_token(*_KEY) is None
                if finish:
                    assert upstream.revoked
            finally:
                upstream.release.set()
                await mcp_oauth.close_mcp_oauth_state(app.state)

    asyncio.run(run())
    expected = "revocation_succeeded" if finish else "upstream_revoke_cancelled"
    assert any(expected in record.message for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)
    assert "refresh-shutdown-sentinel" not in caplog.text
    assert "access-shutdown-sentinel" not in caplog.text


def test_revocation_cap_and_shutdown_are_per_host(
    tmp_path: Any,
    sqlite_backend_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = [sqlite_backend_factory(str(tmp_path / f"host-{i}.db")) for i in range(2)]
    monkeypatch.setattr(mcp_oauth, "_REVOKE_UPSTREAM_TASKS_MAX", 1)
    monkeypatch.setattr(mcp_oauth, "_REVOKE_UPSTREAM_DRAIN_TIMEOUT", 0.01)

    async def run() -> None:
        async with _Upstream("revoke") as upstream:
            hosts = [await _host(store, upstream.origin) for store in stores]
            clients = [app.state.mcp_oauth_http_client for app, _ in hosts]
            try:
                for app, tokens in hosts:
                    response = await _disconnect(app)
                    assert response.status_code == 204
                    assert tokens.get_user_token(*_KEY) is None
                await _until(lambda: upstream.requests.count("/revoke") == 2)
                tasks = [
                    next(iter(mcp_oauth._upstream_revocations(app.state).tasks)) for app, _ in hosts
                ]
                await mcp_oauth.close_mcp_oauth_state(hosts[0][0].state)
                assert tasks[0].cancelled() and clients[0].is_closed
                assert not tasks[1].done() and not clients[1].is_closed
                upstream.release.set()
                await asyncio.wait_for(asyncio.shield(tasks[1]), 5)
                assert not tasks[1].cancelled()
            finally:
                upstream.release.set()
                for app, _ in hosts:
                    await mcp_oauth.close_mcp_oauth_state(app.state)

    asyncio.run(run())


def test_disconnect_during_shutdown_preserves_local_delete(
    tmp_path: Any,
    sqlite_backend_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = sqlite_backend_factory(str(tmp_path / "late.db"))
    entered, release = threading.Event(), threading.Event()

    async def run() -> None:
        async with _Upstream("revoke") as upstream:
            app, tokens = await _host(storage, upstream.origin)
            delete = tokens.delete_user_token

            def held_delete(*args: Any, **kwargs: Any) -> Any:
                entered.set()
                assert release.wait(5)
                return delete(*args, **kwargs)

            monkeypatch.setattr(tokens, "delete_user_token", held_delete)
            request = asyncio.create_task(_disconnect(app))
            try:
                await _until(entered.is_set)
                await mcp_oauth.close_mcp_oauth_state(app.state)
                release.set()
                response = await asyncio.wait_for(request, 5)
                assert response.status_code == 204
                assert tokens.get_user_token(*_KEY) is None
                assert not upstream.requests
                assert not mcp_oauth._upstream_revocations(app.state).tasks
                events = storage.list_audit_events(action="mcp_server.oauth.token_revoked")
                detail = json.loads(events[0]["detail"])
                assert detail["upstream_revoke_outcome"] == "shutting_down"
            finally:
                release.set()
                await asyncio.wait_for(request, 5)
                await mcp_oauth.close_mcp_oauth_state(app.state)

    asyncio.run(run())


def test_revoke_cancellation_budget_is_bounded_and_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(mcp_oauth, "_REVOKE_UPSTREAM_DRAIN_TIMEOUT", 0)
    monkeypatch.setattr(mcp_oauth, "_REVOKE_UPSTREAM_CANCEL_TIMEOUT", 0.01)

    async def run() -> None:
        state = SimpleNamespace()
        await mcp_oauth.initialize_mcp_oauth_state(state)
        client = state.mcp_oauth_http_client
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_cleanup() -> None:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await release.wait()

        task = asyncio.create_task(slow_cleanup())
        tasks = mcp_oauth._upstream_revocations(state).tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        try:
            await entered.wait()
            await asyncio.wait_for(mcp_oauth.close_mcp_oauth_state(state), 1)
            assert task.cancelling() == 1 and not task.done()
            assert client.is_closed
            assert "upstream_revoke_shutdown_timeout" in caplog.text
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert not tasks

    asyncio.run(run())


def _outcome(call: Callable[[], object]) -> object:
    try:
        return call()
    except Exception as exc:
        return exc


@pytest.mark.parametrize(
    ("entrypoint", "phase"),
    [
        ("add", "initialize"),
        ("reconnect", "initialize"),
        ("refresh", "tools/list"),
        ("call_tool", "tools/call"),
        ("call_tool_reconnect", "initialize"),
    ],
)
def test_shutdown_stops_waiting_sync_calls(
    entrypoint: str, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown cancels the work a sync caller waits on, on the running loop: the caller gets
    the shutdown error at once instead of waiting out its timeout (``add_server_sync`` has
    none), and nothing is left on the loop for garbage collection to close. A tool call that
    shutdown stops while it is still reconnecting its server has not started."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})

    async def run() -> None:
        async with _Upstream("none") as upstream:
            manager = MCPClientManager({})
            manager._user_token_sweep_s = 0
            manager._static_health_check_s = 0
            await asyncio.to_thread(manager.start)
            loop, thread = manager._loop, manager._thread
            assert loop is not None and thread is not None
            cfg = {"type": "http", "url": upstream.origin + "/mcp"}
            calls: dict[str, Callable[[], object]] = {
                "add": lambda: manager.add_server_sync("srv", cfg),
                "reconnect": lambda: manager.reconnect_sync("srv"),
                "refresh": lambda: manager.refresh_sync("srv"),
                "call_tool": lambda: manager.call_tool_sync("mcp__srv__example", {}),
                "call_tool_reconnect": lambda: manager.call_tool_sync("mcp__srv__example", {}),
            }

            async def evict_session() -> None:
                # What a dead transport does: the next call reconnects before it is sent.
                manager._drop_static_session_and_stamp("srv", manager._static_servers["srv"])

            try:
                if entrypoint != "add":
                    added = await asyncio.to_thread(manager.add_server_sync, "srv", cfg)
                    assert added["connected"], added
                if entrypoint == "call_tool_reconnect":
                    await asyncio.wrap_future(
                        asyncio.run_coroutine_threadsafe(evict_session(), loop)
                    )
                upstream.phase = phase
                waiting = asyncio.ensure_future(asyncio.to_thread(_outcome, calls[entrypoint]))
                await asyncio.wait_for(upstream.entered.wait(), 5)
                started = time.monotonic()
                await asyncio.to_thread(manager.shutdown)
                # Inside an owner's graceful window: no step waits one out.
                assert time.monotonic() - started < manager._OWNER_CLOSE_GRACE_S
                outcome = await asyncio.wait_for(waiting, 5)
                assert loop.is_closed() and not thread.is_alive()
                assert not asyncio.all_tasks(loop)
                if entrypoint in ("add", "reconnect"):
                    assert isinstance(outcome, dict)
                    assert outcome["connected"] is False
                    assert outcome["error"] == "MCP client is shutting down"
                else:
                    assert isinstance(outcome, MCPShutdownError)
                    assert outcome.started is (entrypoint != "call_tool_reconnect")
            finally:
                upstream.release.set()
                await asyncio.to_thread(manager.shutdown)
        gc.collect()  # an abandoned coroutine would be closed here, and fail the test

    asyncio.run(run())


def test_shutdown_error_marks_only_work_the_drain_cancelled() -> None:
    """Work shutdown cancels raises the shutdown error, marked as started. Work its caller gave
    up on before shutdown stays a plain cancel, since nothing would retrieve an error from it,
    and work submitted after shutdown is refused before its coroutine is even created."""
    manager = MCPClientManager({})
    manager._user_token_sweep_s = 0
    manager._static_health_check_s = 0
    manager.start()
    tasks: list[asyncio.Task[Any]] = []

    async def _forever() -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        await asyncio.Event().wait()

    try:
        abandoned = manager._submit_root(_forever)
        assert poll_until(lambda: len(tasks) == 1)
        abandoned.cancel()
        assert poll_until(tasks[0].done)
        drained = manager._submit_root(_forever)
        assert poll_until(lambda: len(tasks) == 2)
    finally:
        manager.shutdown()
    with pytest.raises(MCPShutdownError) as stopped:
        drained.result(timeout=5)
    assert stopped.value.started is True
    # The tasks on the loop, not the callers' futures: a caller's cancel marks its own future
    # cancelled whatever the task then does.
    assert tasks[0].cancelled()
    assert isinstance(tasks[1].exception(), MCPShutdownError)
    created: list[object] = []
    with pytest.raises(MCPShutdownError) as refused:
        manager._submit_root(lambda: created.append(1) or _forever())
    assert refused.value.started is False
    assert created == []


def test_shutdown_waits_for_an_owner_whose_teardown_it_cut_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A removal takes its server's transport owner off state, asks it to close and waits for
    it. When shutdown cancels that wait, the drain waits for the owner itself: the
    session-termination request the owner is still sending finishes before the loop closes."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})

    async def run() -> None:
        async with _Upstream("none", session_id="shutdown-session") as upstream:
            manager = MCPClientManager({})
            manager._user_token_sweep_s = 0
            manager._static_health_check_s = 0
            await asyncio.to_thread(manager.start)
            loop, thread = manager._loop, manager._thread
            assert loop is not None and thread is not None
            cfg = {"type": "http", "url": upstream.origin + "/mcp"}
            try:
                added = await asyncio.to_thread(manager.add_server_sync, "srv", cfg)
                assert added["connected"], added
                upstream.phase = "delete"
                removing = asyncio.ensure_future(
                    asyncio.to_thread(manager.remove_server_sync, "srv")
                )
                await asyncio.wait_for(upstream.entered.wait(), 5)
                stopping = asyncio.ensure_future(asyncio.to_thread(manager.shutdown))
                await _until(lambda: not manager._accepting_work)
                await asyncio.sleep(0.1)  # time for the drain to cancel the removal's wait
                assert not stopping.done()
                upstream.release.set()
                await asyncio.wait_for(stopping, 10)
                assert loop.is_closed() and not thread.is_alive()
                assert not asyncio.all_tasks(loop)
                assert await asyncio.wait_for(removing, 5) is False
            finally:
                upstream.release.set()
                await asyncio.to_thread(manager.shutdown)
        gc.collect()  # an abandoned coroutine would be closed here, and fail the test

    asyncio.run(run())


@pytest.mark.parametrize("path", ["static", "pool"])
def test_shutdown_waits_for_an_owner_whose_connect_its_caller_gave_up_on(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown cancels a connect still waiting for its server's handshake, and the connect's
    cancel arm cancels the transport owner once and waits for it. When the connect's caller
    gives up during that wait, its cancel cuts the wait short. The drain still waits for the
    owner past its graceful window, and never cancels it a second time."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})
    # The owner outlasts its graceful window, so the drain's escalation step meets it.
    monkeypatch.setattr(MCPClientManager, "_OWNER_CLOSE_GRACE_S", 0.05)
    cancels: list[int] = []
    unwound = threading.Event()
    unwind = asyncio.Event()  # set by the test once the drain has met the owner

    async def owner(
        self: MCPClientManager, key: Any, settings: Any, ready: Any, close_requested: Any
    ) -> None:
        try:
            await asyncio.Event().wait()  # the server never answers the handshake
        except asyncio.CancelledError:
            cancels.append(1)
            try:
                await unwind.wait()  # unwinding the transport takes a while
            except asyncio.CancelledError:
                cancels.append(2)
                raise
            unwound.set()
            raise

    async def no_probe(self: MCPClientManager, key: Any, url: str) -> None:
        return None

    monkeypatch.setattr(MCPClientManager, "_static_transport_owner", owner)
    monkeypatch.setattr(MCPClientManager, "_pool_transport_owner", owner)
    monkeypatch.setattr(MCPClientManager, "_tcp_probe", no_probe)
    manager = MCPClientManager({})
    manager._user_token_sweep_s = 0
    manager._static_health_check_s = 0
    manager.start()
    loop = manager._loop
    assert loop is not None
    cfg = {"type": "http", "url": "https://mcp.example.com/mcp"}
    manager._server_configs["srv"] = cfg

    def connect() -> Coroutine[Any, Any, object]:
        if path == "static":
            return manager._connect_one_locked("srv", cfg)
        return manager._connect_one_pool(("user-1", "srv"), cfg, "synthetic-token")

    try:
        connecting = manager._submit_root(connect)
        assert poll_until(lambda: bool(manager._owners))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            stopping = executor.submit(manager.shutdown)
            assert poll_until(lambda: bool(cancels))
            assert connecting.cancel()  # the caller's own timeout, while the arm waits
            assert poll_until(lambda: not manager._root_tasks)
            time.sleep(0.2)  # the drain's escalation step meets the owner still unwinding
            with contextlib.suppress(RuntimeError):  # a loop already closed fails below
                loop.call_soon_threadsafe(unwind.set)
            stopping.result(timeout=10)
    finally:
        manager.shutdown()
    assert unwound.is_set()
    assert cancels == [1]
    assert loop.is_closed()
    assert not asyncio.all_tasks(loop)


def test_shutdown_cancels_a_registered_owner_that_ignores_its_close_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered transport owner that does not answer shutdown's request to close gets its
    graceful window, then exactly one cancel, and the drain waits for its unwind before the
    loop closes."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})
    monkeypatch.setattr(MCPClientManager, "_OWNER_CLOSE_GRACE_S", 0.05)
    manager = MCPClientManager({})
    manager._user_token_sweep_s = 0
    manager._static_health_check_s = 0
    manager.start()
    loop = manager._loop
    assert loop is not None
    cancels: list[int] = []
    unwound = threading.Event()

    async def ignores_its_close() -> None:
        try:
            await asyncio.Event().wait()  # never watches its close request
        except asyncio.CancelledError:
            cancels.append(1)
            try:
                await asyncio.sleep(0.3)  # unwinding the transport takes a while
            except asyncio.CancelledError:
                cancels.append(2)
                raise
            unwound.set()
            raise

    async def register() -> None:
        close_requested = asyncio.Event()
        owner = asyncio.create_task(ignores_its_close())
        manager._track_owner(owner, close_requested)
        manager._static_servers["srv"] = StaticServerState(
            name="srv", session=MagicMock(), owner_task=owner, close_requested=close_requested
        )

    try:
        asyncio.run_coroutine_threadsafe(register(), loop).result(5)
    finally:
        manager.shutdown()
    assert cancels == [1]
    assert unwound.is_set()
    assert not asyncio.all_tasks(loop)


def test_shutdown_stops_the_health_loop_and_the_token_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The static health loop and the token sweep run for the manager's life; shutdown stops
    both, so neither is left on the closed loop."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})
    manager = MCPClientManager({})
    manager._static_health_check_s = 30.0
    manager._user_token_sweep_s = 240.0
    manager.start()
    loop = manager._loop
    assert loop is not None
    assert poll_until(
        lambda: (
            manager._static_health_task is not None and manager._user_token_sweep_task is not None
        )
    )
    manager.shutdown()
    assert loop.is_closed()
    assert not asyncio.all_tasks(loop)


def test_shutdown_releases_a_start_still_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown during ``start()``'s connect pass releases ``start()`` at once instead of after
    its 30s wait, records no initialization error, and leaves nothing on the loop."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})
    entered = threading.Event()

    async def owner(self: MCPClientManager, name: str, cfg: Any, ready: Any, close: Any) -> None:
        entered.set()
        await asyncio.Event().wait()  # the server never answers the handshake

    async def no_probe(self: MCPClientManager, key: Any, url: str) -> None:
        return None

    monkeypatch.setattr(MCPClientManager, "_static_transport_owner", owner)
    monkeypatch.setattr(MCPClientManager, "_tcp_probe", no_probe)
    manager = MCPClientManager({"srv": {"type": "http", "url": "https://mcp.example.com/mcp"}})
    manager._user_token_sweep_s = 60
    manager._static_health_check_s = 60
    starting = threading.Thread(target=manager.start, name="mcp-start-test")
    began = time.monotonic()
    starting.start()
    try:
        assert entered.wait(5)
        loop = manager._loop
        assert loop is not None
        manager.shutdown()
        starting.join(10)
        assert not starting.is_alive()
        assert time.monotonic() - began < 5
        assert manager._error is None
        assert loop.is_closed()
        assert not asyncio.all_tasks(loop)
    finally:
        manager.shutdown()
        starting.join(35)


def test_shutdown_starts_no_eviction_loop_for_work_unwinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Work that asks for the pool eviction loop while shutdown is cancelling it (a prime's
    failure arm records an error, which starts the loop) gets none: the drain has already taken
    its snapshot, so a new loop would be left on the closed loop."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})
    manager = MCPClientManager({})
    manager._user_token_sweep_s = 0
    manager._static_health_check_s = 0
    manager.start()
    loop = manager._loop
    assert loop is not None
    entered = threading.Event()

    async def records_a_failure_while_unwinding() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            manager._ensure_eviction_loop()
            raise

    manager._submit_root(records_a_failure_while_unwinding)
    assert entered.wait(5)
    manager.shutdown()
    assert loop.is_closed()
    assert not asyncio.all_tasks(loop)


def test_shutdown_is_not_held_by_a_server_that_never_ends_its_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server issues a session id, never finishes the handshake, and never answers the DELETE
    that ends the session. Shutdown cancels the connect, whose cancel arm spends the transport
    owner's one cancel before that DELETE goes out; the DELETE's own timeout then ends it, so
    the owner unwinds within its escalated wait instead of holding shutdown to its deadline."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})
    monkeypatch.setattr(mcp_client, "_SESSION_END_TIMEOUT_S", 0.5)

    async def run() -> None:
        async with _Upstream(
            "initialize-stream", session_id="held-session", hold_session_end=True
        ) as upstream:
            manager = MCPClientManager({})
            manager._user_token_sweep_s = 0
            manager._static_health_check_s = 0
            await asyncio.to_thread(manager.start)
            loop, thread = manager._loop, manager._thread
            assert loop is not None and thread is not None
            cfg = {"type": "http", "url": upstream.origin + "/mcp"}
            try:
                adding = asyncio.ensure_future(
                    asyncio.to_thread(manager.add_server_sync, "srv", cfg)
                )
                await asyncio.wait_for(upstream.entered.wait(), 5)
                started = time.monotonic()
                await asyncio.to_thread(manager.shutdown)
                assert time.monotonic() - started < manager._OWNER_CANCEL_GRACE_S
                assert loop.is_closed() and not thread.is_alive()
                assert not asyncio.all_tasks(loop)
                added = await asyncio.wait_for(adding, 5)
                assert added["error"] == "MCP client is shutting down"
            finally:
                upstream.release.set()
                await asyncio.to_thread(manager.shutdown)
        gc.collect()  # an abandoned coroutine would be closed here, and fail the test

    asyncio.run(run())


def test_shutdown_fails_callers_when_the_loop_thread_is_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a synchronous call keeps the loop thread busy past every wait, shutdown leaves the
    loop open, and the callers still waiting on its work get the shutdown error instead of
    waiting forever."""
    monkeypatch.setattr(MCPClientManager, "_SHUTDOWN_DEADLINE_S", 0.2)
    monkeypatch.setattr(MCPClientManager, "_SHUTDOWN_RESULT_MARGIN_S", 0.1)
    monkeypatch.setattr(MCPClientManager, "_LOOP_JOIN_TIMEOUT_S", 0.2)
    manager = MCPClientManager({})
    manager._user_token_sweep_s = 0
    manager._static_health_check_s = 0
    manager.start()
    loop, thread = manager._loop, manager._thread
    assert loop is not None and thread is not None
    entered, unblock = threading.Event(), threading.Event()

    async def _forever() -> None:
        entered.set()
        await asyncio.Event().wait()

    waiting = manager._submit_root(_forever)
    assert entered.wait(5)
    loop.call_soon_threadsafe(lambda: unblock.wait(10))  # the stuck synchronous call
    try:
        manager.shutdown()
        assert thread.is_alive()  # left open: closing a running loop raises
        with pytest.raises(MCPShutdownError) as stopped:
            waiting.result(timeout=0)
        assert stopped.value.started is True
    finally:
        unblock.set()
        thread.join(5)
        # Unblocked, the loop ran its queued stop; finish what it left and close it. The root
        # completing now finds its caller's future already failed, which asyncio logs.
        left = asyncio.all_tasks(loop)
        for task in left:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*left, return_exceptions=True))
        loop.close()


def test_shutdown_stops_work_whose_own_timeout_is_expiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``asyncio.timeout`` cancels its task when it expires, and turns that cancel into
    ``TimeoutError`` when the task resumes. When shutdown's drain runs in between, it still
    stops the work, which raises the shutdown error instead of catching a timeout and carrying
    on past the drain."""
    manager = MCPClientManager({})
    manager._user_token_sweep_s = 0
    manager._static_health_check_s = 0
    manager.start()
    loop = manager._loop
    assert loop is not None
    steps: list[asyncio.Timeout] = []
    timed_out: list[bool] = []
    parked, unpark, queued = threading.Event(), threading.Event(), threading.Event()

    async def pass_with_a_bounded_step() -> None:
        try:
            async with asyncio.timeout(None) as step:
                steps.append(step)
                await asyncio.Event().wait()
        except TimeoutError:
            timed_out.append(True)  # a pass carries on with its next step here
        await asyncio.Event().wait()

    def park() -> None:
        # The step's bound expires while the loop is parked. Its timer fires once the loop
        # runs again, after the drain queued meanwhile, so the drain's first step runs between
        # the expiry and the task resuming.
        steps[0].reschedule(asyncio.get_running_loop().time() + 0.05)
        parked.set()
        assert unpark.wait(5)

    submit = asyncio.run_coroutine_threadsafe

    def submit_noting_the_drain(coro: Any, target: asyncio.AbstractEventLoop) -> Any:
        future = submit(coro, target)
        if coro.__qualname__ == "MCPClientManager._drain_and_close":
            queued.set()
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit_noting_the_drain)
    try:
        passing = manager._submit_root(pass_with_a_bounded_step)
        assert poll_until(lambda: bool(steps))
        loop.call_soon_threadsafe(park)
        assert parked.wait(5)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            stopping = executor.submit(manager.shutdown)
            assert queued.wait(5)
            time.sleep(0.1)  # past the step's bound, which the parked loop cannot fire yet
            unpark.set()
            stopping.result(timeout=30)
    finally:
        unpark.set()
        manager.shutdown()
    with pytest.raises(MCPShutdownError) as stopped:
        passing.result(timeout=0)
    assert stopped.value.started is True
    assert timed_out == []


def test_shutdown_fails_callers_of_work_left_on_the_closed_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Work still unwinding when shutdown's deadline passes is left on the loop, which then
    closes, so nothing would ever resolve its caller's future (``add_server_sync`` waits without
    a timeout). Shutdown fails those futures with the shutdown error instead."""
    monkeypatch.setattr(MCPClientManager, "_SHUTDOWN_DEADLINE_S", 0.2)
    manager = MCPClientManager({})
    manager._user_token_sweep_s = 0
    manager._static_health_check_s = 0
    manager.start()
    entered = threading.Event()

    async def slow_to_stop() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(30)  # an unwind that outlasts the deadline
            raise

    waiting = manager._submit_root(slow_to_stop)
    assert entered.wait(5)
    manager.shutdown()
    with pytest.raises(MCPShutdownError) as stopped:
        waiting.result(timeout=0)
    assert stopped.value.started is True
    del waiting
    gc.collect()  # the task left on the closed loop reports here, not in a later test


class _QueuedLock(asyncio.Lock):
    """Reads as free, yet an acquire waits: a lock just released to a waiter not yet resumed."""

    def locked(self) -> bool:
        return False

    async def acquire(self) -> bool:  # type: ignore[override]
        await asyncio.Event().wait()
        return True


def test_idle_eviction_lets_a_cancel_end_its_lock_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The idle eviction waits briefly for a pool entry's lock and gives up when the wait expires.
    A cancel during that wait (shutdown's drain) ends the eviction instead of being taken for the
    wait expiring, which would send the eviction loop back to sleep past the drain's deadline."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})
    manager = MCPClientManager({})
    key = ("user-1", "pool-srv")

    async def run() -> object:
        manager._user_pool_entries[key] = PoolEntryState(key=key, open_lock=_QueuedLock())
        evicting = asyncio.create_task(manager._close_pool_entry_if_idle(key))
        await asyncio.sleep(0)  # now waiting for the lock
        evicting.cancel()
        [outcome] = await asyncio.gather(evicting, return_exceptions=True)
        return outcome

    assert isinstance(asyncio.run(run()), asyncio.CancelledError)


def test_reconnect_on_a_loop_shutdown_closed_still_returns_its_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch's reconnect that finishes just before shutdown closes the loop returns its
    session without the catalog refresh it schedules after a reconnect; the dispatch is then
    refused with the shutdown error instead of failing on the closed loop."""
    monkeypatch.setattr("turnstone.core.mcp_client.load_config", lambda *_: {})
    manager = MCPClientManager({"srv": {"type": "stdio", "command": "echo"}})
    session = MagicMock()
    reconnected: concurrent.futures.Future[object] = concurrent.futures.Future()
    reconnected.set_result(session)
    closed = asyncio.new_event_loop()
    closed.close()
    manager._loop = closed
    monkeypatch.setattr(manager, "_submit_root", lambda _factory: reconnected)
    assert manager._cb_auto_reconnect("srv") is session


def test_shutdown_deadline_fits_the_stop_budget() -> None:
    """Shutdown's deadline outlasts the slowest unwind of cancelled work (a child reap, then a
    teardown's graceful and escalated waits) plus a full escalated wait for an owner that work
    leaves running. With its wait for admitted submissions, the thread's margin on that deadline
    and its join of the loop thread, MCP shutdown stays inside a 30s stop budget."""
    slowest_unwind = (
        MCPClientManager._OWNER_CANCEL_GRACE_S
        + MCPClientManager._OWNER_CLOSE_GRACE_S
        + MCPClientManager._OWNER_CANCEL_GRACE_S
    )
    assert (
        slowest_unwind + MCPClientManager._OWNER_CANCEL_GRACE_S
        <= MCPClientManager._SHUTDOWN_DEADLINE_S
    )
    assert (
        MCPClientManager._ADMISSION_WAIT_S
        + MCPClientManager._SHUTDOWN_DEADLINE_S
        + MCPClientManager._SHUTDOWN_RESULT_MARGIN_S
        + MCPClientManager._LOOP_JOIN_TIMEOUT_S
        < 30.0
    )
    # The request that ends a session cannot keep an owner past its escalated wait.
    assert mcp_client._SESSION_END_TIMEOUT_S < MCPClientManager._OWNER_CANCEL_GRACE_S


def test_loop_work_is_submitted_only_through_the_tracking_helper() -> None:
    """Only ``_submit_root`` and shutdown's own phase may submit coroutines to the loop: work
    submitted any other way is invisible to shutdown's drain, and left for garbage collection
    to close on the stopped loop."""
    tree = ast.parse(Path(mcp_client.__file__).read_text())
    sites: list[str] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "run_coroutine_threadsafe":
                    sites.append(scope)
            inner = (
                child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            )
            visit(child, inner)

    visit(tree, "<module>")
    assert sorted(sites) == ["_submit_root", "shutdown"]
