"""Console-side multiplexer for PostgreSQL ``LISTEN``/``NOTIFY`` events.

Holds a single dedicated listen connection (via :meth:`StorageBackend.listen`),
drains it on a listener thread, and fans notifications out to per-channel
handlers on a dedicated dispatch thread so a slow handler doesn't back up
the connection.

Consumers register at construction time by passing their channel in
:attr:`channels`, then call :meth:`subscribe` to attach a handler.
Registering an undeclared channel raises — the construction list is the
single source of truth so wire-in is explicit (each future consumer
touches the dispatcher construction call site at
``turnstone/console/server.py::main`` to add its channel).

On connection loss the listener wakes its handlers with a synthetic
``Notify(channel, payload="reconcile", pid=0)`` so every consumer
re-reads the underlying rows; their normal "reconcile on any wake-up"
code path covers both real notifications and reconnect recovery
identically.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger
from turnstone.core.storage._notify import Notify, NotifyConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from turnstone.core.storage._protocol import StorageBackend


log = get_logger(__name__)


# Backoff (seconds) between reconnect attempts after :class:`NotifyConnectionError`.
# Doubles each failure, capped at the max — long enough that a Postgres outage
# doesn't burn CPU on reconnect spins, short enough that recovery is fast.
_RECONNECT_BACKOFF_INITIAL: float = 1.0
_RECONNECT_BACKOFF_MAX: float = 30.0

# Poll cadence on the listener thread.  Short enough that ``stop`` lands
# promptly without joining a long-blocked notifies() call; long enough
# that we don't burn CPU on empty polls.
_LISTENER_POLL_TIMEOUT: float = 1.0

# Cap on the inter-thread dispatch queue.  Drops oldest if a slow handler
# falls behind (logs once per drop bucket).  Sized larger than the expected
# steady-state notification rate (services trigger fires only on
# register/restart/deregister — order of hundreds per hour at the 100-node
# design ceiling).
_DISPATCH_QUEUE_MAX: int = 1024


class NotifyDispatcher:
    """Holds the dedicated listen connection and fans events to handlers.

    Lifecycle: construct with the declared channel list, attach
    handlers via :meth:`subscribe`, then call :meth:`start`.  :meth:`stop`
    closes the connection and joins the worker threads.  Idempotent in
    both directions so console teardown can call stop unconditionally.
    """

    def __init__(self, storage: StorageBackend, channels: Iterable[str]) -> None:
        ch_list = [str(c) for c in channels if c]
        if not ch_list:
            msg = "NotifyDispatcher requires at least one declared channel"
            raise ValueError(msg)
        self._storage = storage
        self._channels: list[str] = list(dict.fromkeys(ch_list))  # de-dupe, preserve order
        self._handlers: dict[str, list[Callable[[Notify], None]]] = {
            ch: [] for ch in self._channels
        }
        self._handlers_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._started = False
        self._stopping = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._dispatch_queue: queue.Queue[Notify | None] = queue.Queue(maxsize=_DISPATCH_QUEUE_MAX)
        self._drop_count = 0
        # Set inside :meth:`_listener_loop` after each successful
        # ``storage.listen`` open; cleared on disconnect.  Callers use
        # :meth:`wait_until_ready` after :meth:`start` to block until the
        # listener is actually listening (matters when the next caller
        # action is a ``notify`` whose delivery requires the LISTEN to
        # already be in place — e.g. tests, or any startup-path traffic
        # that should be reactive from the first event).
        self._listener_ready = threading.Event()

    @property
    def channels(self) -> list[str]:
        """Snapshot copy of declared channels."""
        return list(self._channels)

    def subscribe(self, channel: str, handler: Callable[[Notify], None]) -> Callable[[], None]:
        """Attach ``handler`` to ``channel``; return an unsubscribe callable.

        Safe to call before or after :meth:`start`.  Raises if the
        channel was not declared at construction time — the channel
        list is fixed so the dispatcher knows up-front which LISTENs
        to issue (consumers added in follow-up PRs touch the
        construction call site).
        """
        if channel not in self._handlers:
            msg = (
                f"channel {channel!r} not declared at construction; "
                f"declared channels: {sorted(self._handlers)}"
            )
            raise ValueError(msg)
        with self._handlers_lock:
            self._handlers[channel].append(handler)

        def _unsubscribe() -> None:
            with self._handlers_lock, contextlib.suppress(ValueError):
                self._handlers[channel].remove(handler)

        return _unsubscribe

    def start(self) -> None:
        """Open the listen stream and start the listener + dispatch threads.

        Idempotent — repeat calls log a debug line and return without
        spawning a second listener.
        """
        with self._lifecycle_lock:
            if self._started:
                log.debug("notify_dispatcher.start_noop_already_started")
                return
            self._started = True
            self._stopping.clear()
            # Clear ready so a stop/start cycle's wait_until_ready only
            # returns True after the new listener has actually opened.
            self._listener_ready.clear()
            self._listener_thread = threading.Thread(
                target=self._listener_loop,
                name="notify-dispatcher-listener",
                daemon=True,
            )
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_loop,
                name="notify-dispatcher-dispatch",
                daemon=True,
            )
            self._listener_thread.start()
            self._dispatch_thread.start()
        log.info(
            "notify_dispatcher.started",
            channels=self._channels,
        )

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        """Block until the listener has opened its stream, or ``timeout`` elapses.

        Returns ``True`` when the listener is ready (``LISTEN`` issued
        for every declared channel on PG; subscriber queues registered
        on SQLite), ``False`` on timeout.  Cleared automatically on
        disconnect — call again after a reconnect to wait for the next
        successful reopen.

        Doesn't replace :meth:`start` — call ``start()`` first, then
        ``wait_until_ready()`` for the explicit sync point.  Production
        startup typically doesn't need this (the first real event tends
        to arrive well after the listener is up); tests use it to close
        the start-vs-notify race window.
        """
        return self._listener_ready.wait(timeout=timeout)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and join the worker threads.

        Idempotent — safe to call multiple times.  Workers exit on the
        next iteration of their poll loops; :meth:`stop` blocks up to
        ``timeout`` seconds per thread before giving up (the threads are
        daemons so the process can exit regardless).
        """
        with self._lifecycle_lock:
            if not self._started:
                return
            self._stopping.set()
            listener = self._listener_thread
            dispatcher = self._dispatch_thread
            # Sentinel wakes the dispatch loop out of queue.get().
            with contextlib.suppress(queue.Full):
                self._dispatch_queue.put_nowait(None)
        if listener is not None:
            listener.join(timeout=timeout)
        if dispatcher is not None:
            dispatcher.join(timeout=timeout)
        with self._lifecycle_lock:
            self._listener_thread = None
            self._dispatch_thread = None
            self._started = False
        log.info("notify_dispatcher.stopped")

    # ------------------------------------------------------------------
    # Internal threading
    # ------------------------------------------------------------------

    def _listener_loop(self) -> None:
        """Drain the storage stream onto the dispatch queue, reconnecting on loss.

        After any disconnect — whether surfaced through the stream's
        :class:`NotifyConnectionError` (post-open ``poll`` failure) or
        through the generic exception path (``psycopg.connect`` /
        initial ``LISTEN`` execute failures during reopen, which are
        NOT wrapped by the stream) — the loop sets a ``reconcile_pending``
        flag, waits the backoff, then enqueues one synthetic ``reconcile``
        notify per channel ONLY after the next stream successfully
        reopens.  Handlers see the synthetic notify and re-read the
        relevant rows on the same code path they use for any real event,
        closing the missed-notification window regardless of which
        exception type caused the disconnect.
        """
        backoff = _RECONNECT_BACKOFF_INITIAL
        reconcile_pending = False
        while not self._stopping.is_set():
            try:
                with self._storage.listen(self._channels) as stream:
                    log.debug(
                        "notify_dispatcher.stream_open",
                        channels=self._channels,
                    )
                    # Stream is open — reset backoff for the next outage
                    # and flush any pending reconcile so consumers see a
                    # wake-up against a now-live DB.
                    backoff = _RECONNECT_BACKOFF_INITIAL
                    if reconcile_pending:
                        self._synthesize_reconcile()
                        reconcile_pending = False
                    # Signal ``wait_until_ready`` callers that LISTEN is
                    # in place (PG) / subscriber queues are bound
                    # (SQLite).  Must come AFTER the synthesize so any
                    # post-reconnect reconcile reaches handlers before
                    # the caller assumes "fresh notifies will deliver".
                    self._listener_ready.set()
                    while not self._stopping.is_set():
                        batch = stream.poll(_LISTENER_POLL_TIMEOUT)
                        for n in batch:
                            self._enqueue(n)
            except NotifyConnectionError as exc:
                if self._stopping.is_set():
                    return
                self._listener_ready.clear()
                log.warning(
                    "notify_dispatcher.connection_lost",
                    error=str(exc),
                    backoff_seconds=backoff,
                )
                reconcile_pending = True
                if self._stopping.wait(backoff):
                    return
                backoff = min(backoff * 2.0, _RECONNECT_BACKOFF_MAX)
            except Exception:
                if self._stopping.is_set():
                    return
                self._listener_ready.clear()
                log.exception("notify_dispatcher.listener_unexpected_error")
                reconcile_pending = True
                if self._stopping.wait(backoff):
                    return
                backoff = min(backoff * 2.0, _RECONNECT_BACKOFF_MAX)
        log.debug("notify_dispatcher.listener_exiting")

    def _synthesize_reconcile(self) -> None:
        """Push one synthetic ``reconcile`` notify per channel on reconnect.

        Reconcile-on-wake is the same logic handlers run for any real
        notification, so a single synthetic event per channel covers
        any notifications missed during the connection-loss window.
        """
        for ch in self._channels:
            self._enqueue(Notify(channel=ch, payload="reconcile", pid=0))

    def _enqueue(self, notify: Notify) -> None:
        """Put a notify on the dispatch queue, dropping oldest on overflow."""
        try:
            self._dispatch_queue.put_nowait(notify)
        except queue.Full:
            # Drop oldest to make room — a slow handler shouldn't be able
            # to silently block the listener thread.  Log once per power
            # of two so a sustained backpressure problem shows up
            # in logs without flooding.
            self._drop_count += 1
            if self._drop_count & (self._drop_count - 1) == 0:
                log.warning(
                    "notify_dispatcher.dispatch_queue_full_dropping_oldest",
                    drops_total=self._drop_count,
                    channel=notify.channel,
                )
            with contextlib.suppress(queue.Empty):
                self._dispatch_queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._dispatch_queue.put_nowait(notify)

    def _dispatch_loop(self) -> None:
        """Pull notifies off the queue and invoke handlers per channel.

        Notifies queued on the same channel coalesce per dispatch batch:
        after blocking ``get()`` returns one notify, the loop drains
        whatever else is already queued and collapses to one
        ``per-channel`` notify before invoking handlers.  The payload is
        signal-only by design (handlers reconcile by re-reading the
        underlying rows), so N same-channel notifies have the same
        observable effect as one — coalescing turns an N-node deploy
        burst into a single ``_discover_nodes`` per channel instead of N.

        Each handler runs under exception suppression so one buggy
        consumer can't take down the dispatch thread.
        """
        while not self._stopping.is_set():
            try:
                first = self._dispatch_queue.get(timeout=_LISTENER_POLL_TIMEOUT)
            except queue.Empty:
                continue
            if first is None:
                # Sentinel from :meth:`stop`.
                return
            # Coalesce by channel: keep the most recent payload per
            # channel from this drain batch.  Drops a stop sentinel
            # silently — the next loop iteration will see _stopping set
            # and exit anyway, so we don't need to re-queue the sentinel.
            per_channel: dict[str, Notify] = {first.channel: first}
            stop_seen = False
            while True:
                try:
                    nxt = self._dispatch_queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop_seen = True
                    continue
                per_channel[nxt.channel] = nxt
            for notify in per_channel.values():
                with self._handlers_lock:
                    handlers = list(self._handlers.get(notify.channel, ()))
                for handler in handlers:
                    t0 = time.monotonic()
                    try:
                        handler(notify)
                    except Exception:
                        log.exception(
                            "notify_dispatcher.handler_failed",
                            channel=notify.channel,
                        )
                    else:
                        elapsed_ms = (time.monotonic() - t0) * 1000.0
                        if elapsed_ms > 100.0:
                            log.debug(
                                "notify_dispatcher.handler_slow",
                                channel=notify.channel,
                                elapsed_ms=round(elapsed_ms, 1),
                            )
            if stop_seen:
                return
        log.debug("notify_dispatcher.dispatch_exiting")
