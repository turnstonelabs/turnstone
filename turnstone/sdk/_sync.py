"""Synchronous execution helper for the turnstone SDK.

Maintains a background event loop on a daemon thread so that async
client methods can be called from synchronous code without the
overhead of ``asyncio.run()`` per call.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine, Iterator

T = TypeVar("T")

_STOP = object()


async def _safe_anext(agen: AsyncIterator[T]) -> T | object:
    """Advance *agen* without raising StopAsyncIteration across a thread boundary."""
    try:
        return await agen.__anext__()
    except StopAsyncIteration:
        return _STOP


class _SyncRunner:
    """Run async coroutines synchronously via a persistent background loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
                self._thread.start()
            return self._loop

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Submit *coro* to the background loop and block for the result."""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    def run_iter(self, async_gen: AsyncIterator[T]) -> Iterator[T]:
        """Synchronously iterate over an async generator."""
        loop = self._ensure_loop()
        while True:
            future = asyncio.run_coroutine_threadsafe(_safe_anext(async_gen), loop)
            result = future.result()
            if result is _STOP:
                return
            yield result  # type: ignore[misc]

    def close(self) -> None:
        """Shut down the background event loop."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._loop.close()
            self._loop = None
            self._thread = None
