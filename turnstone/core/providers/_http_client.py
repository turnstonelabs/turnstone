"""SDK HTTP clients whose ``close()`` wakes a body read blocked in another thread.

:meth:`ModelRegistry.reload` closes a provider's SDK client from the admin
thread while a worker may be blocked reading a streamed response. Closing the
socket's descriptor does not interrupt that read: on Linux it ends with the
server's next byte or FIN, on macOS not even then, so the worker waits out the
read timeout (600 s by default). ``shutdown(2)`` wakes the read on both at once,
so these clients shut each socket down before closing it, and the blocked read
fails immediately with the transport error ``transport_guarded`` already treats
as retryable.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import ipaddress
import socket
import threading
import time
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

import httpcore2
import httpx2
import structlog
from httpcore2._backends.sync import SyncStream

if TYPE_CHECKING:
    import ssl
    from collections.abc import Callable, Iterable, Iterator

log = structlog.get_logger(__name__)


_request_deadline: contextvars.ContextVar[HTTPDeadline | None] = contextvars.ContextVar(
    "http_request_deadline", default=None
)

# A system resolver cannot be cancelled. Keep its work bounded even if callers
# time out: a permit returns only when DNS actually finishes, never on abandonment.
_DNS_SLOTS = threading.BoundedSemaphore(4)


def _connect_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpcore2.ConnectTimeout("HTTP connection deadline exceeded")
    return remaining


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _resolve_tcp(host: str, port: int, deadline: float) -> list[Any]:
    if _is_ip_literal(host):
        # AI_NUMERICHOST never queries a resolver, so a numeric address needs no
        # slot and is not held up by lookups stuck on other hosts.
        _connect_remaining(deadline)
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    slots = _DNS_SLOTS
    if not slots.acquire(timeout=_connect_remaining(deadline)):
        raise httpcore2.ConnectTimeout("HTTP connection deadline exceeded during DNS")
    result: Future[list[Any]] = Future()

    def resolve() -> None:
        try:
            result.set_result(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except BaseException as exc:
            result.set_exception(exc)
        finally:
            slots.release()

    try:
        _connect_remaining(deadline)
        threading.Thread(target=resolve, name="provider-dns", daemon=True).start()
    except BaseException:
        slots.release()
        raise
    try:
        addresses = result.result(timeout=_connect_remaining(deadline))
    except TimeoutError as exc:
        raise httpcore2.ConnectTimeout("HTTP connection deadline exceeded during DNS") from exc
    _connect_remaining(deadline)
    return addresses


class HTTPDeadline:
    """A request's network budget, shared across upload, headers and response reads.

    Socket timeouts alone restart on each partial send. The timer also shuts down
    a stream while this request owns an I/O operation, waking those inner loops.
    Registration ends before the connection can return to the pool, so an old
    deadline cannot close a connection another request has borrowed.
    """

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self._lock = threading.Lock()
        self._active: set[_ShutdownStream] = set()
        self._on_expire: Callable[[], None] | None = None
        self._closed = False
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self) -> None:
        with self._lock:
            if self._closed:
                return
            # Keep registration locked through shutdown: an operation cannot
            # finish unregistering and lend its connection to another caller.
            for stream in self._active:
                stream.close()
            on_expire = self._on_expire
        if on_expire is not None:
            on_expire()

    def on_expire(self, close: Callable[[], None]) -> None:
        """Also release an owned response when its consumer pauses between reads."""
        with self._lock:
            if self._closed:
                return
            self._on_expire = close
            expired = time.monotonic() >= self.deadline
        if expired:
            close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._on_expire = None
        self._timer.cancel()
        if threading.current_thread() is not self._timer:
            self._timer.join()

    @contextlib.contextmanager
    def activate(self) -> Iterator[None]:
        token = _request_deadline.set(self)
        try:
            yield
        finally:
            _request_deadline.reset(token)

    def remaining(self, error: type[Exception]) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise error("HTTP request deadline exceeded")
        return remaining

    @contextlib.contextmanager
    def operation(self, stream: _ShutdownStream, error: type[Exception]) -> Iterator[None]:
        with self._lock:
            self.remaining(error)
            self._active.add(stream)
        try:
            try:
                yield
            except Exception as exc:
                if time.monotonic() >= self.deadline:
                    raise error("HTTP request deadline exceeded") from exc
                raise
            self.remaining(error)
        finally:
            with self._lock:
                self._active.discard(stream)


@contextlib.contextmanager
def http_request_deadline(deadline: float | None) -> Iterator[None]:
    if deadline is None:
        yield
        return
    budget = HTTPDeadline(deadline)
    try:
        with budget.activate():
            yield
            budget.remaining(httpx2.ReadTimeout)
    finally:
        budget.close()


def _bounded_timeout(timeout: float | None, error: type[Exception]) -> float | None:
    budget = _request_deadline.get()
    if budget is None:
        return timeout
    remaining = budget.remaining(error)
    return remaining if timeout is None else min(timeout, remaining)


class _ShutdownStream(httpcore2.NetworkStream):
    """The backend's own stream, except that ``close()`` shuts the socket down first.

    The descriptor is closed only once no read or write is in progress. A read that
    has not yet started waiting when the descriptor closes waits on nothing until
    its timeout, which on macOS caught a few percent of reads closed just after they
    began. So a close that finds I/O in progress leaves the descriptor to the last
    call out, which the shutdown has already woken.
    """

    def __init__(self, inner: httpcore2.NetworkStream) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._io_in_progress = 0
        self._close_pending = False

    @contextlib.contextmanager
    def _io(self, error: type[Exception], timeout: float | None) -> Iterator[float | None]:
        with self._lock:
            self._io_in_progress += 1
        try:
            budget = _request_deadline.get()
            if budget is None:
                yield timeout
            else:
                with budget.operation(self, error):
                    remaining = budget.remaining(error)
                    yield remaining if timeout is None else min(timeout, remaining)
        finally:
            with self._lock:
                self._io_in_progress -= 1
                if self._close_pending and not self._io_in_progress:
                    self._close_pending = False
                    self._inner.close()

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        with self._io(httpcore2.ReadTimeout, timeout) as bounded:
            return self._inner.read(max_bytes, bounded)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        with self._io(httpcore2.WriteTimeout, timeout) as bounded:
            self._inner.write(buffer, bounded)

    def close(self) -> None:
        sock = self._inner.get_extra_info("socket")
        if isinstance(sock, socket.socket):
            # ENOTCONN once the peer has gone, EBADF once closed; neither stops the close.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        with self._lock:
            if self._io_in_progress:
                self._close_pending = True
            else:
                self._inner.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.NetworkStream:
        inner = None
        try:
            with self._io(httpcore2.ConnectTimeout, timeout) as bounded:
                inner = self._inner.start_tls(
                    ssl_context,
                    server_hostname,
                    bounded,
                )
            return _ShutdownStream(inner)
        except BaseException:
            if inner is not None:
                inner.close()
            raise

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _ShutdownBackend(httpcore2.NetworkBackend):
    def __init__(self, inner: httpcore2.NetworkBackend) -> None:
        self._inner = inner

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        budget = _request_deadline.get()
        # Only replace the standard backend: custom backends may implement
        # routing or transports that a direct socket would silently bypass.
        if budget is not None and type(self._inner) is httpcore2.SyncBackend:
            deadline = budget.deadline
            if timeout is not None:
                deadline = min(deadline, time.monotonic() + timeout)
            return self._connect_tcp(host, port, deadline, local_address, socket_options)
        stream = self._inner.connect_tcp(
            host,
            port,
            timeout=_bounded_timeout(timeout, httpcore2.ConnectTimeout),
            local_address=local_address,
            socket_options=socket_options,
        )
        return _ShutdownStream(stream)

    def _connect_tcp(
        self,
        host: str,
        port: int,
        deadline: float,
        local_address: str | None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None,
    ) -> httpcore2.NetworkStream:
        try:
            addresses = _resolve_tcp(host, port, deadline)
            local_addresses = (
                _resolve_tcp(local_address, 0, deadline) if local_address is not None else None
            )
            options = tuple(socket_options or ())
            last_error: OSError | None = None
            for family, kind, protocol, _, address in addresses:
                _connect_remaining(deadline)
                try:
                    sock = socket.socket(family, kind, protocol)
                except OSError as exc:
                    # An unavailable address family must not prevent trying a
                    # later family, matching socket.create_connection.
                    last_error = exc
                    continue
                stream = _ShutdownStream(SyncStream(sock))
                try:
                    with stream._io(
                        httpcore2.ConnectTimeout, _connect_remaining(deadline)
                    ) as bounded:
                        sock.settimeout(bounded)
                        if local_addresses is not None:
                            local = next(
                                (item[4] for item in local_addresses if item[0] == family), None
                            )
                            if local is None:
                                raise OSError("No local address matches the remote address family")
                            sock.bind(local)
                        sock.connect(address)
                        for option in options:
                            sock.setsockopt(*option)
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        _connect_remaining(deadline)
                    return stream
                except OSError as exc:
                    last_error = exc
                    stream.close()
                except BaseException:
                    stream.close()
                    raise
            if last_error is not None:
                raise last_error
            raise OSError("getaddrinfo returned no addresses")
        except TimeoutError as exc:
            raise httpcore2.ConnectTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore2.ConnectError(str(exc)) from exc

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        stream = self._inner.connect_unix_socket(
            path,
            timeout=_bounded_timeout(timeout, httpcore2.ConnectTimeout),
            socket_options=socket_options,
        )
        return _ShutdownStream(stream)


def _connection_pool(transport: httpx2.BaseTransport) -> Any:
    if not isinstance(transport, httpx2.HTTPTransport):
        raise TypeError(f"expected an httpx2.HTTPTransport, got {type(transport).__name__}")
    # httpx2 keeps its httpcore2 pool, and the pool its network backend, as private
    # attributes; neither has a public seam for swapping the backend after construction.
    pool = transport._pool
    if not isinstance(pool._network_backend, httpcore2.NetworkBackend):
        raise TypeError(f"unexpected network backend {type(pool._network_backend).__name__}")
    return pool


@functools.cache
def _warn_left_unwrapped(reason: str) -> None:
    log.warning(
        "providers.http_client: closing a model client will not wake a blocked stream read",
        reason=reason,
    )


def shutdown_on_close(client: httpx2.Client) -> httpx2.Client:
    """Make ``client.close()`` wake reads blocked on its connections in other threads.

    Covers the default transport and every proxy mount, including the ones the
    SDKs build from the proxy environment variables. Call it before the client's
    first request: a connection keeps the backend it was opened with.

    If an httpx2 or httpcore2 release moves the private attributes this relies on,
    the client is returned as the SDK built it, with one warning, rather than failing
    every model. tests/test_provider_http_client.py pins the shape so CI notices.
    """
    try:
        pools = [
            _connection_pool(transport)
            for transport in (client._transport, *client._mounts.values())
            if transport is not None
        ]
    except (AttributeError, TypeError) as exc:
        _warn_left_unwrapped(f"{type(exc).__name__}: {exc}")
        return client
    for pool in pools:
        if not isinstance(pool._network_backend, _ShutdownBackend):
            pool._network_backend = _ShutdownBackend(pool._network_backend)
    return client
