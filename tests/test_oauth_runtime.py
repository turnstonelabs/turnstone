"""OAuth ownership, real bridges, cancellation and bounded process teardown."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import dataclasses
import gc
import inspect
import ipaddress
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock

import httpx
import pytest

from tests._oauth_runtime_helpers import run_adapter
from tests._oidc_test_helpers import make_oidc_config
from tests.conftest import make_mcp_token_cipher
from turnstone.core import mcp_oauth, model_oauth
from turnstone.core.ip_classify import AddressLane
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.model_backend_auth import (
    BackendAuthUnavailableError,
    resolve_model_backend_auth_token,
)
from turnstone.core.model_oauth import OAuthModelTokenClient
from turnstone.core.model_registry import ModelConfig
from turnstone.core.oauth import http as oauth_http
from turnstone.core.oauth import locking, oidc
from turnstone.core.oauth.context import OAuthContext, TokenCoordination, oauth_context
from turnstone.core.oauth.runtime import (
    OAuthRuntime,
    ensure_oauth_runtime,
    shutdown_oauth_runtime,
)
from turnstone.core.oauth.work import OAuthUnavailableError, durable_write

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend


async def _settled(runtime: OAuthRuntime) -> None:
    """Await operation/worker evidence on its owner loop, excluding this observer."""
    async with asyncio.timeout(5):
        while True:
            pending = [
                t for t in runtime._operations if t is not asyncio.current_task() and not t.done()
            ]
            work = runtime._work
            assert work is not None
            if not pending and not work.drains and not any(not f.done() for f in work.workers):
                return
            await asyncio.sleep(0)


def _state(backend: StorageBackend) -> SimpleNamespace:
    backend.create_mcp_server(
        server_id="runtime-server",
        name="runtime-server",
        transport="streamable-http",
        url="https://mcp.test/mcp",
        auth_type="oauth_user",
        oauth_client_id="client",
    )
    store = MCPTokenStore(backend, make_mcp_token_cipher())
    store.create_user_token(
        "user",
        "runtime-server",
        access_token="old",
        refresh_token="rt-old",
        expires_at=(datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S"),
        scopes="read",
        as_issuer="https://as.test",
        audience="https://mcp.test/mcp",
    )
    return SimpleNamespace(
        auth_storage=backend,
        oauth_context=OAuthContext(
            storage=backend, token_store=store, oidc_config=make_oidc_config()
        ),
        mcp_oauth_coordination=TokenCoordination(),
    )


def _runtime(context: OAuthContext, handler: Any = None) -> OAuthRuntime:
    def client() -> httpx.AsyncClient:
        return (
            httpx.AsyncClient(transport=httpx.MockTransport(handler))
            if handler
            else oauth_http.json_http_client()
        )

    runtime = OAuthRuntime(context, client_factory=client)
    context.runtime = runtime
    runtime.start()
    return runtime


@pytest.mark.parametrize("cancel_kind", ["cancel", "deadline"])
def test_cancelled_custodial_write_excludes_same_key_until_settled(
    backend: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    cancel_kind: str,
) -> None:
    """A cancelled MCP caller releases its lock; durable work stays serialized."""
    state = _state(backend)
    context = oauth_context(state)
    store = context.token_store
    assert store is not None
    started, release, queued = threading.Event(), threading.Event(), threading.Event()
    phases: list[str] = []
    posts: list[asyncio.AbstractEventLoop] = []
    request_context = contextvars.ContextVar("runtime_write_context", default="missing")
    worker_context: list[str] = []

    def post(request: httpx.Request) -> httpx.Response:
        posts.append(asyncio.get_running_loop())
        return httpx.Response(
            200, json={"access_token": "new", "refresh_token": "rt-new", "expires_in": 3600}
        )

    runtime = _runtime(context, post)
    metadata = oauth_http.ASMetadata(
        "https://as.test",
        "https://as.test/authorize",
        "https://as.test/token",
        None,
        None,
        None,
        (),
        (),
    )
    monkeypatch.setattr(
        mcp_oauth, "discover_authorization_server", AsyncMock(return_value=metadata)
    )
    write = store.update_user_token_after_refresh

    def blocked_write(*args: Any, **kwargs: Any) -> Any:
        worker_context.append(request_context.get())
        phases.append("write-start")
        started.set()
        assert release.wait(5), "test did not release durable write"
        result = write(*args, **kwargs)
        phases.append("write-end")
        return result

    monkeypatch.setattr(store, "update_user_token_after_refresh", blocked_write)
    acquire = backend.acquire_advisory_lock_sync

    @contextlib.contextmanager
    def observed_advisory(key: str):
        with acquire(key):
            phases.append("advisory-enter")
            try:
                yield
            finally:
                phases.append("advisory-exit")

    monkeypatch.setattr(backend, "acquire_advisory_lock_sync", observed_advisory)

    class ObservedLock(asyncio.Lock):
        async def acquire(self) -> Literal[True]:
            if self.locked():
                queued.set()
            return await super().acquire()

        def release(self) -> None:
            phases.append("local-exit")
            super().release()

    async def install() -> None:
        context.coordination.locks[("user", "runtime-server")] = ObservedLock()

    runtime.call_sync(install)
    call = runtime.call
    if cancel_kind == "deadline":
        submit = runtime._submit

        def started_submit(factory: Any) -> Any:
            future = submit(factory)
            # Begin the short caller deadline only once its controlled write
            # is in flight; thread scheduling speed is not under test.
            assert started.wait(3)
            return future

        monkeypatch.setattr(runtime, "_submit", started_submit)

        async def short_call(factory: Any, **kwargs: Any) -> Any:
            return await call(factory, timeout=0.1)

        monkeypatch.setattr(runtime, "call", short_call)

    async def run() -> None:
        token = request_context.set("request-context")
        first = asyncio.create_task(
            mcp_oauth.get_user_access_token_classified(
                app_state=state, user_id="user", server_name="runtime-server"
            )
        )
        second: asyncio.Task[Any] | None = None
        try:
            assert await asyncio.to_thread(started.wait, 3)
            if cancel_kind == "cancel":
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(first, 2)
            else:
                assert (await asyncio.wait_for(first, 2)).kind == "refresh_failed_transient"
                monkeypatch.setattr(runtime, "call", call)
            assert not state.mcp_oauth_coordination.locks[("user", "runtime-server")].locked()
            assert state.mcp_oauth_coordination.backoff == {}
            assert phases == ["advisory-enter", "write-start"]
            second = asyncio.create_task(
                mcp_oauth.get_user_access_token_classified(
                    app_state=state, user_id="user", server_name="runtime-server"
                )
            )
            async with asyncio.timeout(3):
                while not queued.is_set() and len(posts) < 2:
                    await asyncio.sleep(0)
            assert len(posts) == 1, "same-key durable operations overlapped"
            assert queued.is_set() and not second.done()
            release.set()
            result = await asyncio.wait_for(second, 5)
            assert result.kind == "token" and result.token == "new"
            await call(lambda: _settled(runtime))
            assert phases[:4] == ["advisory-enter", "write-start", "write-end", "advisory-exit"]
            assert phases[4] == "local-exit"
            assert len(posts) == 1 and posts[0] is runtime._loop
            assert state.mcp_oauth_coordination.backoff == {}
            assert worker_context == ["request-context"]
            assert store.get_user_token("user", "runtime-server")["refresh_token"] == "rt-new"
        finally:
            release.set()
            request_context.reset(token)
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
            await call(lambda: _settled(runtime))

    run_adapter(run())


@pytest.mark.parametrize("path", ["mcp-user", "mcp-obo", "model-user", "model-app"])
@pytest.mark.parametrize("unavailable", ["stopped", "submit-race", "never-started"])
def test_unavailable_runtime_preserves_grants_and_policy(
    backend: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    unavailable: str,
) -> None:
    state = _state(backend)
    context = oauth_context(state)
    if unavailable == "never-started":
        shutdown_oauth_runtime(state)
        runtime = ensure_oauth_runtime(state)
        assert runtime is not None and runtime._thread is None
    else:
        runtime = _runtime(context)
    before = backend.get_oauth_token("user", "runtime-server")
    if path == "mcp-obo":
        backend.update_mcp_server(
            "runtime-server", auth_type="oauth_obo", oauth_audience="api://mcp"
        )
        context.token_store.upsert_oidc_credential(
            "user", context.oidc_config.issuer, refresh_token="rt-credential"
        )
    rejected: list[Any] = []
    if unavailable == "stopped":
        runtime.shutdown()
    elif unavailable == "submit-race":
        submit = asyncio.run_coroutine_threadsafe

        def reject(operation: Any, loop: asyncio.AbstractEventLoop) -> Any:
            if loop is runtime._loop:
                rejected.append(operation)
                raise RuntimeError("loop closed between check and submission")
            return submit(operation, loop)

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", reject)
    if path.startswith("mcp"):
        lookup = (
            mcp_oauth.get_user_access_token_classified
            if path == "mcp-user"
            else mcp_oauth.get_obo_access_token_classified
        )
        result = run_adapter(lookup(app_state=state, user_id="user", server_name="runtime-server"))
        assert result.kind == "refresh_failed_transient"
        assert not state.mcp_oauth_coordination.locks[("user", "runtime-server")].locked()
        assert state.mcp_oauth_coordination.backoff == {}
    else:
        client = OAuthModelTokenClient(context)
        mode = "entra_app" if path == "model-app" else "entra_obo"
        for static_key in ("operator-key", ""):
            config = ModelConfig(
                alias="model",
                model="model",
                provider="openai",
                base_url="https://model.test",
                api_key=static_key,
                auth_mode=mode,
                obo_audience="api://model",
            )
            kwargs = dict(
                config=config,
                alias="model",
                principal_id="user",
                config_store=None,
                mint_client=client,
            )
            if static_key:
                assert resolve_model_backend_auth_token(**kwargs) is None
            else:
                with pytest.raises(BackendAuthUnavailableError):
                    resolve_model_backend_auth_token(**kwargs)
    assert backend.get_oauth_token("user", "runtime-server") == before
    assert context.coordination.backoff == {}
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in rejected)
    assert (len(rejected) > 0) == (unavailable == "submit-race")


def test_failed_runtime_initialization_logs_cause_and_is_not_retried(
    backend: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state(backend)
    attempts = 0

    def fail_client() -> httpx.AsyncClient:
        nonlocal attempts
        attempts += 1
        raise ValueError("client initialization failed")

    monkeypatch.setattr(oauth_http, "json_http_client", fail_client)
    with pytest.raises(OAuthUnavailableError, match="initialization failed"):
        ensure_oauth_runtime(state)
    runtime = ensure_oauth_runtime(state)
    assert runtime is not None
    with pytest.raises(OAuthUnavailableError, match="unavailable"):
        runtime.call_sync(lambda: asyncio.sleep(0))
    assert attempts == 1
    assert runtime._thread is not None and not runtime._thread.is_alive()
    failures = [record for record in caplog.records if "initialization failed" in record.message]
    assert len(failures) == 1
    assert "ValueError: client initialization failed" in caplog.text


def test_runtime_reuses_shared_oidc_holder_after_host_rediscovery(
    backend: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = OAuthContext(
        storage=backend,
        token_store=MCPTokenStore(backend, make_mcp_token_cipher()),
        oidc_config=make_oidc_config(enabled=False, discovery_retryable=True),
    )
    host = SimpleNamespace(oauth_context=context)
    calls: list[str] = []

    def request(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json={"access_token": "model-token", "expires_in": 300})

    runtime = _runtime(context, request)
    healed = dataclasses.replace(
        context.oidc_config,
        enabled=True,
        token_endpoint="https://healed.test/token",
        discovery_retryable=False,
    )
    monkeypatch.setattr(oidc, "discover_oidc", AsyncMock(return_value=healed))
    run_adapter(oidc.maybe_rediscover_oidc(host))
    assert context.oidc_config == healed
    assert runtime.context is context
    client = OAuthModelTokenClient(context)
    assert client.mint_app_token_sync(alias="model", audience="api://model") == "model-token"
    assert calls == ["https://healed.test/token"]
    assert ensure_oauth_runtime(host) is runtime
    assert not hasattr(host, "mcp_client")


def _assert_waiter_safe_pruning() -> None:
    async def run() -> None:
        state = TokenCoordination()
        first = locking._refresh_lock_for(state, "u", "key")
        await first.acquire()
        waiter = asyncio.create_task(first.acquire())
        try:
            await asyncio.sleep(0)  # queue the waiter before requesting pruning
            locking._drop_refresh_lock(state, "u", "key")
            first.release()
            # A new arrival must use the same lock while the older waiter is queued.
            assert locking._refresh_lock_for(state, "u", "key") is first, "split queued lock"
            await waiter
            assert locking._refresh_lock_for(state, "u", "key") is first, "split held lock"
            first.release()
            locking._prune_token_lock_when_idle(state, "u", "key", first)
            assert state.locks == {}
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            if first.locked():
                first.release()

    run_adapter(run())


def test_drop_keeps_lock_until_queued_waiter_releases() -> None:
    _assert_waiter_safe_pruning()


def test_unconditional_pop_fails_waiter_safety_control(monkeypatch: pytest.MonkeyPatch) -> None:
    def unconditional_pop(state: TokenCoordination, user: str, key: str) -> None:
        state.locks.pop((user, key), None)

    with monkeypatch.context() as patch:
        patch.setattr(locking, "_drop_refresh_lock", unconditional_pop)
        with pytest.raises(AssertionError, match="split queued lock"):
            _assert_waiter_safe_pruning()


def test_bridge_distinguishes_operation_timeout_from_caller_deadline() -> None:
    runtime = _runtime(OAuthContext())

    async def operation_timeout() -> None:
        raise TimeoutError("operation timed out")

    with pytest.raises(TimeoutError, match="operation timed out"):
        runtime.call_sync(operation_timeout)

    async def run() -> None:
        with pytest.raises(TimeoutError, match="operation timed out"):
            await runtime.call(operation_timeout)
        with pytest.raises(OAuthUnavailableError, match="deadline"):
            await runtime.call(lambda: asyncio.sleep(30), timeout=0.01)

    run_adapter(run())


def test_abandoned_operation_exception_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime(OAuthContext())
    entered = threading.Event()

    async def fail_after_cancel() -> None:
        entered.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise ValueError("abandoned operation failed") from None

    future = runtime._submit(fail_after_cancel)
    assert entered.wait(3)
    future.cancel()
    runtime.call_sync(lambda: _settled(runtime))
    assert any("OAuth operation failed" in record.message for record in caplog.records)


def test_shutdown_retrieves_worker_failure_after_cancellation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed worker is reported once, including the extra drain-phase waiter."""
    runtime = _runtime(OAuthContext())
    runtime.OPERATION_DRAIN_TIMEOUT = 0.01
    entered, release = threading.Event(), threading.Event()
    loop_errors: list[dict[str, Any]] = []
    warn = runtime._warn_outstanding

    async def observe_errors() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: loop_errors.append(context)
        )

    runtime.call_sync(observe_errors)

    def release_in_worker_phase(phase: str) -> None:
        warn(phase)
        if phase == "operations":
            # The drain installs its worker waiter before this queued release
            # can run. No timing guess decides which waiter sees the failure.
            asyncio.get_running_loop().call_soon(release.set)

    monkeypatch.setattr(runtime, "_warn_outstanding", release_in_worker_phase)

    def failed_write() -> None:
        entered.set()
        assert release.wait(3)
        raise ValueError("worker failed while draining")

    async def operation() -> None:
        await durable_write(failed_write)

    runtime._submit(operation)
    assert entered.wait(3)
    try:
        runtime.shutdown()
    finally:
        release.set()
    gc.collect()
    assert not loop_errors, [context.get("message") for context in loop_errors]
    assert (
        sum(
            "worker failed while settling cancellation" in record.message
            for record in caplog.records
        )
        == 1
    )
    assert not runtime._thread.is_alive()
    assert not runtime._work.workers and not runtime._operations


@pytest.mark.parametrize("phase", ["acquire", "write", "release", "queued-release"])
def test_shutdown_drains_started_worker_before_http_close(
    backend: StorageBackend,
    phase: str,
) -> None:
    runtime = _runtime(OAuthContext())
    entered, release = threading.Event(), threading.Event()
    order: list[str] = []
    client = runtime.context.http_client
    assert client is not None
    close = client.aclose

    async def observed_close() -> None:
        order.append("http-close")
        await close()

    client.aclose = observed_close

    class CM:
        def __enter__(self) -> None:
            if phase == "acquire":
                entered.set()
                assert release.wait(3)
            order.append("enter")

        def __exit__(self, *args: Any) -> None:
            if phase == "release":
                entered.set()
                assert release.wait(3)
            order.append("release")

    storage = SimpleNamespace(acquire_advisory_lock_sync=lambda key: CM())
    blocker = None
    if phase == "queued-release":
        work = runtime._work
        assert work is not None
        submit = work.submit

        def block_executor() -> None:
            entered.set()
            assert release.wait(3)

        def queue_release(executor: Any, function: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal blocker
            if function.__name__ == "_exit":
                blocker = executor.submit(block_executor)
            return submit(executor, function, *args, **kwargs)

        work.submit = queue_release

    def write() -> None:
        if phase == "write":
            entered.set()
            assert release.wait(3)
        order.append("write")

    async def operation() -> None:
        async with await locking._acquire_pg_refresh_lock(storage, "u", "key"):
            await durable_write(write)

    future = runtime._submit(operation)
    assert entered.wait(3)
    # Stop from a separate caller while the worker is held; the test thread
    # releases it only after cancellation has reached the runtime operation.
    cancelled = threading.Event()

    async def observe_cancellation() -> None:
        async with asyncio.timeout(2):
            while not any(task.cancelling() for task in runtime._operations):
                await asyncio.sleep(0)
            cancelled.set()

    observer = asyncio.run_coroutine_threadsafe(observe_cancellation(), runtime._loop)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        shutdown = executor.submit(runtime.shutdown)
        try:
            assert cancelled.wait(2)
            observer.result(timeout=1)
            assert not shutdown.done()
            assert "http-close" not in order
        finally:
            release.set()
        shutdown.result(timeout=5)
    assert order == (
        ["enter", "release", "http-close"]
        if phase == "acquire"
        else ["enter", "write", "release", "http-close"]
    )
    assert client.is_closed
    assert runtime._thread is not None and not runtime._thread.is_alive()
    assert runtime._work is not None and not runtime._work.workers and not runtime._work.drains
    assert not runtime._operations
    assert future.done()
    if blocker is not None:
        blocker.result(timeout=1)


@pytest.mark.parametrize("phase", ["acquire", "write", "release"])
def test_shutdown_budget_exhaustion_is_bounded_and_loud(phase: str) -> None:
    # Deliberately leave work beyond the process-exit budget in a child process.
    # No stopped-loop recovery mechanism is part of the production contract.
    script = textwrap.dedent("""
        import asyncio, sys, threading, time
        from types import SimpleNamespace
        from turnstone.core.oauth import locking
        from turnstone.core.oauth.context import OAuthContext
        from turnstone.core.oauth.runtime import OAuthRuntime
        from turnstone.core.oauth.work import durable_write
        runtime = OAuthRuntime(OAuthContext())
        runtime.OPERATION_DRAIN_TIMEOUT = 0.02
        runtime.WORKER_DRAIN_TIMEOUT = 0.02
        runtime.start()
        phase = sys.argv[1]
        entered, release = threading.Event(), threading.Event()
        acquired, exited = threading.Event(), threading.Event()
        owner_threads = []
        executors = set()
        submit = runtime._work.submit
        def track(executor, function, *args, **kwargs):
            executors.add(executor)
            return submit(executor, function, *args, **kwargs)
        runtime._work.submit = track
        def block():
            entered.set()
            assert release.wait(3)
        class CM:
            def __enter__(self):
                if phase == "acquire":
                    block()
                owner_threads.append(threading.get_ident())
                acquired.set()
            def __exit__(self, *args):
                if phase == "release":
                    block()
                owner_threads.append(threading.get_ident())
                exited.set()
        cm = CM()
        storage = SimpleNamespace(acquire_advisory_lock_sync=lambda key: cm)
        def write():
            if phase == "write":
                block()
        async def operation():
            async with await locking._acquire_pg_refresh_lock(storage, "u", "key"):
                await durable_write(write)
        runtime._submit(operation)
        assert entered.wait(2)
        start = time.monotonic()
        try:
            runtime.shutdown()
            assert time.monotonic() - start < 1
            assert not runtime._thread.is_alive()
            assert any(not work.done() for work in runtime._work.workers)
        finally:
            release.set()
            for worker in runtime._work.workers:
                worker.result(timeout=2)
            # The loop deliberately stopped after its budget. Complete the
            # fixture's transaction on its original worker, then join every
            # executor; production does not gain a stopped-loop recovery service.
            if acquired.is_set() and not exited.is_set():
                lock_executor = next(e for e in executors if e is not runtime._work.executor)
                lock_executor.submit(cm.__exit__, None, None, None).result(timeout=2)
            for executor in executors:
                executor.shutdown(wait=True)
            assert exited.is_set() and len(set(owner_threads)) == 1
            assert not any(t.name.startswith(("oauth-", "mcp-pg-")) for t in threading.enumerate())
        print("bounded shutdown verified")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, phase], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bounded shutdown verified" in result.stdout
    assert "cleanup left to process teardown" in result.stdout + result.stderr


@pytest.mark.parametrize("path", ["discovery", "refresh", "model-obo", "model-app"])
def test_shutdown_cancels_inflight_http_before_closing_client(
    backend: StorageBackend, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    state = _state(backend)
    context = oauth_context(state)
    context.token_store.upsert_oidc_credential(
        "user", context.oidc_config.issuer, refresh_token="credential"
    )
    entered = threading.Event()
    order: list[str] = []

    async def request(req: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            order.append("request-exit")
        raise AssertionError("request was not cancelled")

    runtime = _runtime(context, request)
    client = context.http_client
    close = client.aclose

    async def observed_close() -> None:
        assert asyncio.get_running_loop() is runtime._loop
        order.append("http-close")
        await close()

    monkeypatch.setattr(client, "aclose", observed_close)
    if path == "discovery":
        # Leave PostgreSQL's localhost DNS intact while stubbing OAuth resolution.
        monkeypatch.setattr(
            "turnstone.core.oauth.ssrf.resolve_and_classify",
            lambda _hostname: [(AddressLane.PUBLIC, ipaddress.ip_address("93.184.216.34"))],
        )
    elif path == "refresh":
        metadata = oauth_http.ASMetadata(
            "https://as.test",
            "https://as.test/authorize",
            "https://as.test/token",
            None,
            None,
            None,
            (),
            (),
        )
        monkeypatch.setattr(
            mcp_oauth, "discover_authorization_server", AsyncMock(return_value=metadata)
        )
    before = backend.get_oauth_token("user", "runtime-server")

    async def run() -> None:
        if path.startswith("model"):
            adapter = OAuthModelTokenClient(context)
            method = adapter.mint_app_token_sync
            kwargs: dict[str, Any] = {"alias": "model", "audience": "api://model"}
            if path == "model-obo":
                method = adapter.mint_model_obo_token_sync
                kwargs["user_id"] = "user"
            task = asyncio.create_task(asyncio.to_thread(method, **kwargs))
        else:
            task = asyncio.create_task(
                mcp_oauth.get_user_access_token_classified(
                    app_state=state, user_id="user", server_name="runtime-server"
                )
            )
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            await asyncio.to_thread(runtime.shutdown)
            result = await asyncio.wait_for(task, 2)
            assert (
                result is None
                if path.startswith("model")
                else result.kind == "refresh_failed_transient"
            )
            assert order == ["request-exit", "http-close"]
            assert not runtime._thread.is_alive()
            assert backend.get_oauth_token("user", "runtime-server") == before
            assert context.coordination.backoff == {}
            assert state.mcp_oauth_coordination.backoff == {}
            assert all(not lock.locked() for lock in state.mcp_oauth_coordination.locks.values())
        finally:
            await asyncio.to_thread(runtime.shutdown)
            await asyncio.gather(task, return_exceptions=True)

    run_adapter(run())


def test_closed_http_client_is_unavailable_without_failure_disposition(
    backend: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(backend)
    context = oauth_context(state)
    runtime = _runtime(context)
    metadata = oauth_http.ASMetadata(
        "https://as.test",
        "https://as.test/authorize",
        "https://as.test/token",
        None,
        None,
        None,
        (),
        (),
    )
    monkeypatch.setattr(
        mcp_oauth, "discover_authorization_server", AsyncMock(return_value=metadata)
    )
    runtime.call_sync(context.http_client.aclose)
    before = backend.get_oauth_token("user", "runtime-server")
    result = run_adapter(
        mcp_oauth.get_user_access_token_classified(
            app_state=state, user_id="user", server_name="runtime-server"
        )
    )
    assert result.kind == "refresh_failed_transient"
    assert state.mcp_oauth_coordination.backoff == {}
    assert backend.get_oauth_token("user", "runtime-server") == before


@pytest.mark.parametrize("path", ["discovery", "refresh", "model-obo", "model-app"])
def test_client_closed_during_request_has_no_failure_disposition(
    backend: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    state = _state(backend)
    context = oauth_context(state)
    context.token_store.upsert_oidc_credential(
        "user", context.oidc_config.issuer, refresh_token="credential"
    )
    entered, release = threading.Event(), threading.Event()

    async def request(req: httpx.Request) -> httpx.Response:
        entered.set()
        assert await asyncio.to_thread(release.wait, 3)
        return httpx.Response(400, json={"error": "invalid_grant"})

    runtime = _runtime(context, request)
    if path == "discovery":
        # Leave PostgreSQL's localhost DNS intact while stubbing OAuth resolution.
        monkeypatch.setattr(
            "turnstone.core.oauth.ssrf.resolve_and_classify",
            lambda _hostname: [(AddressLane.PUBLIC, ipaddress.ip_address("93.184.216.34"))],
        )
    elif path == "refresh":
        metadata = oauth_http.ASMetadata(
            "https://as.test",
            "https://as.test/authorize",
            "https://as.test/token",
            None,
            None,
            None,
            (),
            (),
        )
        monkeypatch.setattr(
            mcp_oauth, "discover_authorization_server", AsyncMock(return_value=metadata)
        )
    before = backend.get_oauth_token("user", "runtime-server")

    async def run() -> None:
        if path.startswith("model"):
            client = OAuthModelTokenClient(context)
            kwargs: dict[str, Any] = {"alias": "model", "audience": "api://model"}
            method = client.mint_app_token_sync
            if path == "model-obo":
                method = client.mint_model_obo_token_sync
                kwargs["user_id"] = "user"
            task = asyncio.create_task(asyncio.to_thread(method, **kwargs))
        else:
            task = asyncio.create_task(
                mcp_oauth.get_user_access_token_classified(
                    app_state=state, user_id="user", server_name="runtime-server"
                )
            )
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(context.http_client.aclose(), runtime._loop)
            )
            release.set()
            result = await asyncio.wait_for(task, 3)
            if path.startswith("model"):
                assert result is None
            else:
                assert result.kind == "refresh_failed_transient"
            assert backend.get_oauth_token("user", "runtime-server") == before
            assert context.coordination.backoff == {}
            assert state.mcp_oauth_coordination.backoff == {}
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    run_adapter(run())


@pytest.mark.parametrize("path", ["obo", "app"])
def test_model_bridge_deadline_cancels_but_retains_started_write(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    runtime = _runtime(OAuthContext())
    entered, release = threading.Event(), threading.Event()
    completed: list[str] = []

    def write() -> None:
        entered.set()
        assert release.wait(3)
        completed.append("write")

    async def mint(**kwargs: Any) -> str:
        await durable_write(write)
        completed.append("result")
        return "late-token"

    name = "_mint_obo_access_token" if path == "obo" else "_mint_app_access_token"
    monkeypatch.setattr(model_oauth, name, mint)
    client = OAuthModelTokenClient(runtime.context)
    submit = runtime._submit

    def started_submit(factory: Any) -> Any:
        future = submit(factory)
        assert entered.wait(3)  # the real future deadline begins after the write starts
        return future

    monkeypatch.setattr(runtime, "_submit", started_submit)
    try:
        kwargs: dict[str, Any] = {"alias": "model", "audience": "api://model", "timeout": 0.05}
        method = client.mint_app_token_sync
        if path == "obo":
            method = client.mint_model_obo_token_sync
            kwargs["user_id"] = "user"
        assert method(**kwargs) is None
        assert entered.is_set() and completed == []

        async def cancelled() -> None:
            async with asyncio.timeout(2):
                while not any(task.cancelling() for task in runtime._operations):
                    await asyncio.sleep(0)

        runtime.call_sync(cancelled)
    finally:
        release.set()
        runtime.call_sync(lambda: _settled(runtime))
    assert completed == ["write"]
