"""Owned executor work whose completion survives cancellation of an awaiter."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import functools
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

log = get_logger(__name__)

_active_work: contextvars.ContextVar[OAuthWork | None] = contextvars.ContextVar(
    "oauth_work", default=None
)


class OAuthUnavailableError(Exception):
    """The runtime cannot complete a request within its caller's lifetime."""


class OAuthWork:
    """Worker futures, their owner-loop waiters and orphan drains owned by one OAuth runtime."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.workers: set[concurrent.futures.Future[Any]] = set()
        self.waiters: set[asyncio.Future[Any]] = set()
        self.drains: set[asyncio.Task[None]] = set()
        self.executor = concurrent.futures.ThreadPoolExecutor(thread_name_prefix="oauth-store")

    def submit[T](
        self,
        executor: concurrent.futures.ThreadPoolExecutor,
        function: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> concurrent.futures.Future[T]:
        """Retain the real future and preserve the submitting context in its worker.

        Called on the owner loop. Every worker gets an owner-loop waiter here, so shutdown can drain
        each worker's outcome through ``waiters``.
        """
        context = contextvars.copy_context()
        call = functools.partial(function, *args, **kwargs)
        future = executor.submit(context.run, call)
        self.workers.add(future)

        def completed(done: concurrent.futures.Future[T]) -> None:
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.workers.discard, done)

        future.add_done_callback(completed)
        self.wrap(future)
        return future

    def wrap[T](self, future: concurrent.futures.Future[T]) -> asyncio.Future[T]:
        """Create a fresh owner-loop waiter and retrieve abandoned waiter failures.

        A waiter receives its worker's outcome one loop hop after the worker's own future completes,
        so it stays in ``waiters`` until that hop has landed and been retrieved. Shutdown drains
        waiters for that reason: a stop in between would leave the failure set and never retrieved.
        """
        wrapped = asyncio.wrap_future(future, loop=self.loop)
        self.waiters.add(wrapped)
        wrapped.add_done_callback(self._retire_waiter)
        return wrapped

    def _retire_waiter(self, waiter: asyncio.Future[Any]) -> None:
        self.waiters.discard(waiter)
        if not waiter.cancelled():
            waiter.exception()

    async def wait[T](
        self, future: concurrent.futures.Future[T], *, settle_on_cancel: bool = True
    ) -> T:
        """Settle submitted work before propagating cancellation past held locks."""
        wrapped = self.wrap(future)
        try:
            # asyncio.wait leaves its input intact when its own awaiter is
            # cancelled. Even a queued release worker must still execute.
            await asyncio.wait((wrapped,))
            return wrapped.result()
        except asyncio.CancelledError:
            if not settle_on_cancel:
                raise
            # Shutdown can cancel an operation whose caller already cancelled
            # it. Each wait gets a fresh wrapper; repeated cancellation cannot
            # turn a completed asyncio waiter into false worker completion.
            while not future.done():
                try:
                    await asyncio.wait((self.wrap(future),))
                except asyncio.CancelledError:
                    continue
            try:
                future.result()
            except Exception:
                log.warning("OAuth worker failed while settling cancellation", exc_info=True)
            raise

    async def write[T](self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await self.wait(self.submit(self.executor, function, *args, **kwargs))

    def drain(self, operation: Coroutine[Any, Any, None]) -> None:
        task = self.loop.create_task(operation, name="oauth-lock-drain")
        self.drains.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self.drains.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                log.error("OAuth lock drain failed", exc_info=error)

        task.add_done_callback(completed)


def current_work() -> OAuthWork:
    work = _active_work.get()
    if work is None:
        raise RuntimeError("OAuth durable work must run inside its runtime")
    return work


async def durable_write[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Submit a protected token/credential write through the current runtime."""
    return await current_work().write(function, *args, **kwargs)
