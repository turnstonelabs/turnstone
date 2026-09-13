"""Shutdown of accepted primes and revocations over real local HTTP (#1147)."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from aiohttp import web
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from tests.conftest import make_mcp_token_cipher
from tests.test_mcp_oauth_handlers import _InjectAuthMiddleware
from turnstone.core import mcp_oauth
from turnstone.core.mcp_client import MCPClientManager
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.oauth.context import oauth_context
from turnstone.core.oauth.oidc import OIDCConfig
from turnstone.core.oauth.runtime import shutdown_oauth_runtime

_KEY = ("user-1", "pool-srv")


async def _until(predicate: Any) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


class _Upstream:
    """An AS and MCP endpoint with event-controlled response boundaries."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
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
        if request.method != "POST":
            return web.Response(status=405)
        body = await request.json()
        if "id" not in body:
            return web.Response(status=202)
        method = body["method"]
        await self.gate(method)
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "shutdown-test", "version": "1"},
            }
        else:
            assert method == "tools/list"
            result = {"tools": [{"name": "example", "inputSchema": {"type": "object"}}]}
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})


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
                    assert "BaseExceptionGroup" in status["discovery_error"]
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
