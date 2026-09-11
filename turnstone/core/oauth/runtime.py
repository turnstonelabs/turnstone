"""Process-owned OAuth execution, bounded bridges and shutdown."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.oauth import http as oauth_http
from turnstone.core.oauth.context import OAuthContext, oauth_context
from turnstone.core.oauth.work import OAuthUnavailableError, OAuthWork, _active_work

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    import httpx

log = get_logger(__name__)


class OAuthRuntime:
    """One execution loop for both OAuth consumers throughout a host's lifetime."""

    CALL_TIMEOUT = 20.0
    # Let async MCP callers wait through PostgreSQL's 30s advisory spin and
    # subsequent discovery/token requests. The synchronous model budget stays 20s.
    ASYNC_CALL_TIMEOUT = 60.0
    OPERATION_DRAIN_TIMEOUT = 5.0
    WORKER_DRAIN_TIMEOUT = 5.0
    CLIENT_CLOSE_TIMEOUT = 5.0
    THREAD_JOIN_TIMEOUT = 5.0

    def __init__(
        self,
        context: OAuthContext,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.context = context
        self._client_factory = client_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._submissions = threading.Lock()
        self._accepting = False
        self._operations: set[asyncio.Task[Any]] = set()
        self._work: OAuthWork | None = None

    def start(self) -> None:
        """Start once; stopped instances are never replaced or restarted."""
        with self._submissions:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(target=loop.run_forever, name="oauth-loop", daemon=True)
            self._thread.start()

            async def initialize() -> None:
                self._work = OAuthWork(loop)
                self.context.http_client = (self._client_factory or oauth_http.json_http_client)()

            future = asyncio.run_coroutine_threadsafe(initialize(), loop)
            try:
                future.result(timeout=self.CALL_TIMEOUT)
            except BaseException:
                loop.call_soon_threadsafe(loop.stop)
                self._thread.join(timeout=self.THREAD_JOIN_TIMEOUT)
                if not self._thread.is_alive():
                    loop.close()
                raise
            self._accepting = True

    async def _run[T](self, factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        task = asyncio.current_task()
        if task is None or self._work is None:
            raise OAuthUnavailableError("OAuth runtime has not initialized its operation owner")
        self._operations.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self._operations.discard(done)
            if (
                not done.cancelled()
                and (error := done.exception()) is not None
                and not isinstance(error, OAuthUnavailableError)
            ):
                log.error("OAuth operation failed", exc_info=error)

        task.add_done_callback(completed)
        token = _active_work.set(self._work)
        try:
            if not self._accepting:
                raise OAuthUnavailableError("OAuth runtime stopped before operation began")
            result = await factory()
            if not self._accepting:
                raise OAuthUnavailableError("OAuth runtime stopped during operation")
            return result
        except RuntimeError as exc:
            client = self.context.http_client
            if client is not None and client.is_closed:
                raise OAuthUnavailableError("OAuth HTTP client closed during shutdown") from exc
            raise
        finally:
            _active_work.reset(token)

    def _submit[T](
        self, factory: Callable[[], Coroutine[Any, Any, T]]
    ) -> concurrent.futures.Future[T]:
        with self._submissions:
            loop = self._loop
            if not self._accepting or loop is None or not loop.is_running() or loop.is_closed():
                raise OAuthUnavailableError("OAuth runtime is unavailable")
            operation = self._run(factory)
            try:
                return asyncio.run_coroutine_threadsafe(operation, loop)
            except RuntimeError as exc:
                operation.close()
                raise OAuthUnavailableError("OAuth runtime rejected submission") from exc

    async def call[T](
        self, factory: Callable[[], Coroutine[Any, Any, T]], *, timeout: float = ASYNC_CALL_TIMEOUT
    ) -> T:
        future = self._submit(factory)
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                return await asyncio.wrap_future(future)
        except TimeoutError:
            if not deadline.expired():
                raise
            future.cancel()
            raise OAuthUnavailableError("OAuth caller deadline expired") from None
        except asyncio.CancelledError:
            future.cancel()
            task = asyncio.current_task()
            if task is not None and not task.cancelling():
                raise OAuthUnavailableError("OAuth runtime cancelled operation") from None
            raise

    def call_sync[T](
        self, factory: Callable[[], Coroutine[Any, Any, T]], *, timeout: float = CALL_TIMEOUT
    ) -> T:
        if self._thread is threading.current_thread():
            raise OAuthUnavailableError("Cannot block the OAuth loop on itself")
        future = self._submit(factory)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # An operation-raised TimeoutError is not a caller deadline.
            if future.done():
                return future.result()
            future.cancel()
            raise OAuthUnavailableError("OAuth caller deadline expired") from None
        except concurrent.futures.CancelledError:
            raise OAuthUnavailableError("OAuth runtime cancelled operation") from None

    def _warn_outstanding(self, phase: str) -> None:
        work = self._work
        log.warning(
            "OAuth shutdown %s budget exhausted: %d operations, %d drains, %d workers; "
            "cleanup left to process teardown",
            phase,
            sum(not task.done() for task in tuple(self._operations)),
            sum(not task.done() for task in tuple(work.drains)) if work is not None else 0,
            sum(not future.done() for future in tuple(work.workers)) if work is not None else 0,
        )

    async def _drain(self) -> None:
        operations = list(self._operations)
        for task in operations:
            task.cancel()
        if operations:
            _, pending = await asyncio.wait(operations, timeout=self.OPERATION_DRAIN_TIMEOUT)
            if pending:
                self._warn_outstanding("operations")

        work = self._work
        if work is not None:
            deadline = asyncio.get_running_loop().time() + self.WORKER_DRAIN_TIMEOUT
            while True:
                pending_work: list[asyncio.Future[Any]] = [
                    task for task in (*self._operations, *work.drains) if not task.done()
                ]
                pending_work.extend(
                    work.wrap(future) for future in work.workers if not future.done()
                )
                if not pending_work:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._warn_outstanding("workers")
                    break
                await asyncio.wait(
                    pending_work, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
            work.executor.shutdown(wait=False)

        client = self.context.http_client
        if client is not None:
            try:
                async with asyncio.timeout(self.CLIENT_CLOSE_TIMEOUT):
                    await client.aclose()
            except Exception:
                log.warning("OAuth HTTP client close failed during shutdown", exc_info=True)

    def shutdown(self) -> None:
        """Quiesce submissions, drain within budgets, close HTTP, and join the loop."""
        with self._submissions:
            self._accepting = False
            loop = self._loop
        if loop is None or loop.is_closed():
            return
        if loop.is_running():
            operation = self._drain()
            try:
                future = asyncio.run_coroutine_threadsafe(operation, loop)
            except RuntimeError:
                operation.close()
            else:
                try:
                    future.result(
                        timeout=self.OPERATION_DRAIN_TIMEOUT
                        + self.WORKER_DRAIN_TIMEOUT
                        + self.CLIENT_CLOSE_TIMEOUT
                        + 1.0
                    )
                except Exception:
                    self._warn_outstanding("cleanup")
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.THREAD_JOIN_TIMEOUT)
            if thread.is_alive():
                log.warning("OAuth loop thread did not stop within its budget; loop left open")
                return
        if not loop.is_running():
            loop.close()


_runtime_create_lock = threading.Lock()


def ensure_oauth_runtime(host: Any) -> OAuthRuntime | None:
    """Lazily start the host's one runtime when encrypted token storage is available."""
    context = oauth_context(host)
    with _runtime_create_lock:
        if context.runtime is None and context.token_store is not None:
            runtime = OAuthRuntime(context)
            context.runtime = runtime
            try:
                runtime.start()
            except Exception as exc:
                log.error("OAuth runtime initialization failed", exc_info=True)
                raise OAuthUnavailableError("OAuth runtime initialization failed") from exc
        return context.runtime


def shutdown_oauth_runtime(host: Any) -> None:
    context = oauth_context(host)
    with _runtime_create_lock:
        # An inert sentinel prevents a late mint from starting the loop after
        # host teardown, including when this host never needed OAuth at runtime.
        if context.runtime is None:
            context.runtime = OAuthRuntime(context)
        runtime = context.runtime
    runtime.shutdown()
