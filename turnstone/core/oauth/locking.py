"""Local and PostgreSQL coordination shared by OAuth consumers."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.oauth.work import OAuthWork, current_work

if TYPE_CHECKING:
    from turnstone.core.oauth.context import TokenCoordination
    from turnstone.core.storage._protocol import StorageBackend
log = get_logger(__name__)


def _refresh_lock_for(state: TokenCoordination, user_id: str, server_name: str) -> asyncio.Lock:
    """Return the shared refresh lock for ``(user_id, server_name)``.

    This in-process ``asyncio.Lock`` provides intra-process
    serialization. Cluster-wide serialization is layered on top via the
    Postgres advisory lock acquired by :func:`_acquire_pg_refresh_lock`.
    Mint and refresh callers take the local lock before the advisory lock.
    """
    loop = asyncio.get_running_loop()
    if state.loop is None:
        state.loop = loop
    elif state.loop is not loop:
        raise RuntimeError("OAuth coordination belongs to a different event loop")
    locks = state.locks
    key = (user_id, server_name)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _drop_refresh_lock(state: TokenCoordination, user_id: str, server_name: str) -> None:
    """Request pruning on the owner loop without splitting queued same-key callers."""
    lock = state.locks.get((user_id, server_name))
    if lock is not None:
        _prune_token_lock_when_idle(state, user_id, server_name, lock)


def _refresh_advisory_key(user_id: str, server_name: str) -> str:
    """Return the advisory-lock key for ``(user_id, server_name)``.

    Hashed via ``pg_advisory_xact_lock(hashtext(...))`` at the storage
    layer; the textual form is what the storage helper hashes, so the
    callers don't need to know about the digest.
    """
    return f"mcp_refresh:{user_id}:{server_name}"


async def _acquire_pg_refresh_lock(
    storage: StorageBackend, user_id: str, server_name: str
) -> contextlib.AbstractAsyncContextManager[None]:
    """Async context manager that serializes the refresh body across nodes.

    On Postgres, holds ``pg_advisory_xact_lock(hashtext(...))`` for the
    duration of the body via :meth:`StorageBackend.acquire_advisory_lock_sync`.
    On SQLite, the storage helper returns ``nullcontext`` and this is
    effectively a no-op — single-node deployments rely on the
    in-process ``asyncio.Lock`` for serialization.

    The blocking SQLAlchemy hops inside the storage context manager are
    routed through a per-acquisition thread executor so the caller's loop
    stays responsive.
    """
    return _PgRefreshLock(storage, _refresh_advisory_key(user_id, server_name))


async def _drain_orphan_pg_lock(
    work: OAuthWork,
    executor: concurrent.futures.ThreadPoolExecutor,
    cm: contextlib.AbstractContextManager[None],
    cf_fut: concurrent.futures.Future[Any],
) -> None:
    """Best-effort cleanup of a ``_PgRefreshLock`` whose ``__aenter__`` raised.

    The worker thread that ran ``cm.__enter__`` may complete *after* the
    awaiter has propagated an exception (typically a cancellation). If
    it ultimately acquired the Postgres advisory lock, the paired
    ``cm.__exit__`` MUST run on the same executor (same OS thread) so
    the connection-bound transaction commits / rolls back on the thread
    that began it. If this drain is cancelled during loop shutdown, an
    acquired connection can remain checked out; engine disposal does not
    guarantee its release.

    ``cf_fut`` retains the underlying ``concurrent.futures.Future``. Cancellation
    of the operation does not establish that this worker finished. The owned
    work helper creates a fresh waiter for the drain and retrieves its outcome
    before submitting the paired release.
    """
    try:
        try:
            await work.wait(cf_fut)
        except Exception:
            # ``cm.__enter__`` raised on the worker — nothing acquired,
            # nothing to release. ``CancelledError`` is intentionally NOT
            # caught: if the drain task itself is cancelled (loop
            # shutdown), it should record as cancelled rather than be
            # silently logged as 'completed normally with no acquire'.
            # Cancelling the drain does not establish that its worker has
            # completed or that any checked-out connection has been released.
            return
        try:
            await work.wait(work.submit(executor, cm.__exit__, None, None, None))
        except Exception:
            # Same rationale as above for ``CancelledError``: don't
            # mask drain-task cancellation as 'drain_exit_failed'.
            log.warning(
                "mcp_server.oauth.pg_refresh_lock_drain_exit_failed",
                exc_info=True,
            )
    finally:
        executor.shutdown(wait=False)


class _PgRefreshLock(contextlib.AbstractAsyncContextManager[None]):
    """Async wrapper around :meth:`StorageBackend.acquire_advisory_lock_sync`.

    Each lock's connection and transaction stay on one OS thread:
    ``__enter__`` begins the transaction and ``__exit__`` commits or rolls it
    back. Each instance allocates a private single-worker
    ``ThreadPoolExecutor`` to satisfy that constraint. A module-global
    single-worker executor would also satisfy thread-affinity, but at
    the cost of serializing every advisory-lock acquire on the node
    behind one thread — different ``(user, server)`` keys would queue
    against each other through the spin loop's 30s timeout window.
    Per-instance executors keep the affinity guarantee while letting
    unrelated refreshes spin on ``pg_try_advisory_xact_lock`` in
    parallel.

    Cancellation between submit and the worker completing
    ``cm.__enter__`` is handled by ``_drain_orphan_pg_lock``. The runtime's work
    helper retains the submitted ``concurrent.futures.Future`` and leaves it
    intact when the acquisition awaiter is cancelled. The drain waits for that
    worker to settle, then runs ``cm.__exit__`` on the same executor when the
    lock did get acquired.
    """

    def __init__(
        self, storage: StorageBackend, key_text: str, *, work: OAuthWork | None = None
    ) -> None:
        self._storage = storage
        self._key_text = key_text
        self._sync_cm: contextlib.AbstractContextManager[None] | None = None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._work = work

    async def __aenter__(self) -> None:
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mcp-pg-refresh-lock"
        )
        cm = self._storage.acquire_advisory_lock_sync(self._key_text)
        work = self._work or current_work()
        self._work = work
        # Submit directly to keep a handle on the underlying
        # ``concurrent.futures.Future``. Each ``asyncio.wrap_future`` here
        # and inside the drain creates an *independent* asyncio Future
        # tied to the same worker outcome, so cancellation of one wrapper
        # doesn't poison the other.
        cf_fut = work.submit(executor, cm.__enter__)
        try:
            await work.wait(cf_fut, settle_on_cancel=False)
        except BaseException:
            work.drain(_drain_orphan_pg_lock(work, executor, cm, cf_fut))
            raise
        self._sync_cm = cm
        self._executor = executor

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        cm = self._sync_cm
        executor = self._executor
        self._sync_cm = None
        self._executor = None
        if cm is None or executor is None:
            return
        work = self._work
        if work is None:
            raise RuntimeError("OAuth lock release has no worker owner")

        def _exit() -> None:
            cm.__exit__(exc_type, exc, tb)

        # Same-thread release is owned until its actual worker completes,
        # including when the operation is cancelled while awaiting it.
        try:
            await work.wait(work.submit(executor, _exit))
        finally:
            executor.shutdown(wait=False)


def _prune_token_lock_when_idle(
    state: TokenCoordination,
    user_id: str,
    cache_server: str,
    lock: asyncio.Lock,
) -> None:
    """Prune a key's lock after queued waiters have had a chance to acquire it."""

    def _drop_if_idle() -> None:
        locks = state.locks
        waiters = getattr(lock, "_waiters", None)
        if locks.get((user_id, cache_server)) is lock and not lock.locked() and not waiters:
            locks.pop((user_id, cache_server), None)

    _drop_if_idle()
    # A released lock with a queued waiter is intentionally retained. Give the
    # waiter priority, then let the last participant prune on its own return.
    if state.locks.get((user_id, cache_server)) is lock:
        asyncio.get_running_loop().call_soon(_drop_if_idle)
