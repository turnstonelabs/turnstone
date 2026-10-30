"""Turnstone's SDK HTTP clients wake a blocked read when closed from another thread."""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from types import SimpleNamespace
from typing import Any

import anthropic
import httpcore2
import httpx2
import openai
import pytest
from httpcore2._backends.sync import SyncStream

from turnstone.core.providers import _http_client as provider_http
from turnstone.core.providers._http_client import (
    HTTPDeadline,
    _ShutdownBackend,
    _ShutdownStream,
    _warn_left_unwrapped,
    http_request_deadline,
    shutdown_on_close,
)


def _backends(client: httpx2.Client) -> list[httpcore2.NetworkBackend]:
    transports = [client._transport, *client._mounts.values()]
    return [t._pool._network_backend for t in transports if isinstance(t, httpx2.HTTPTransport)]


@pytest.mark.parametrize(
    "default_client", [openai.DefaultHttpxClient, anthropic.DefaultHttpxClient]
)
def test_default_transport_and_proxy_mounts_shut_down_on_close(
    monkeypatch: pytest.MonkeyPatch, default_client: type[httpx2.Client]
) -> None:
    """Every transport the SDK's default client builds, including the proxy mounts
    it derives from the environment, gets the shutdown-on-close backend; a bump of
    httpx2 or httpcore2 that moves the pool or backend attribute fails here."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    client = shutdown_on_close(default_client())
    try:
        backends = _backends(client)
        assert len(backends) >= 3, "default transport plus one mount per proxy variable"
        assert all(isinstance(backend, _ShutdownBackend) for backend in backends)
    finally:
        client.close()


def test_shutdown_on_close_is_idempotent() -> None:
    client = shutdown_on_close(shutdown_on_close(openai.DefaultHttpxClient()))
    try:
        (backend,) = _backends(client)
        assert isinstance(backend, _ShutdownBackend)
        assert not isinstance(backend._inner, _ShutdownBackend)
    finally:
        client.close()


def _unwrapped_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "will not wake" in r.getMessage()]


def test_an_unknown_transport_leaves_the_client_as_built(caplog: pytest.LogCaptureFixture) -> None:
    """A transport this module can't wrap fails safe to the SDK's own client, warned once."""
    _warn_left_unwrapped.cache_clear()
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200))
    with caplog.at_level(logging.WARNING, logger="turnstone.core.providers._http_client"):
        for _ in range(2):
            client = httpx2.Client(transport=transport)
            assert shutdown_on_close(client) is client
            assert client.get("http://model.invalid/").status_code == 200
    [message] = _unwrapped_warnings(caplog)
    assert "MockTransport" in message


def test_a_moved_private_attribute_leaves_the_client_as_built(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An httpx2 release that renames the pool's backend wraps nothing, not some transports."""
    _warn_left_unwrapped.cache_clear()
    client = httpx2.Client(mounts={"http://proxy.invalid": httpx2.HTTPTransport()})
    default_backend = client._transport._pool._network_backend
    renamed = next(iter(client._mounts.values()))._pool
    renamed._backend = renamed.__dict__.pop("_network_backend")
    try:
        with caplog.at_level(logging.WARNING, logger="turnstone.core.providers._http_client"):
            assert shutdown_on_close(client) is client
        assert client._transport._pool._network_backend is default_backend
        [message] = _unwrapped_warnings(caplog)
        assert "AttributeError" in message
    finally:
        renamed._network_backend = renamed._backend
        client.close()


class _HeldRead(httpcore2.NetworkStream):
    """A stream whose read blocks until released, recording when it is closed."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.reading = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.reading.set()
        self.release.wait(timeout=5.0)
        return b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        raise AssertionError("not written")

    def close(self) -> None:
        self.closed.set()

    def get_extra_info(self, info: str) -> Any:
        return self._sock if info == "socket" else None


def test_close_during_a_read_shuts_down_now_and_closes_when_the_read_returns() -> None:
    """Closing the descriptor under a read that has not yet started waiting leaves it
    waiting on nothing until its timeout, so the close is left to the read on its way
    out; the shutdown, which is what wakes the read, still happens at once."""
    ours, peer = socket.socketpair()
    peer.settimeout(5.0)
    inner = _HeldRead(ours)
    stream = _ShutdownStream(inner)
    reader = threading.Thread(target=stream.read, args=(1,))
    reader.start()
    try:
        assert inner.reading.wait(timeout=5.0)
        stream.close()
        assert peer.recv(1) == b"", "the shutdown reached the peer"
        assert not inner.closed.is_set(), "the descriptor closed under the read"
        inner.release.set()
        reader.join(timeout=5.0)
        assert inner.closed.is_set()
    finally:
        inner.release.set()
        reader.join(timeout=5.0)
        ours.close()
        peer.close()


def test_close_with_no_read_in_progress_closes_at_once_and_again_harmlessly() -> None:
    ours, peer = socket.socketpair()
    inner = _HeldRead(ours)
    stream = _ShutdownStream(inner)
    try:
        stream.close()
        assert inner.closed.is_set()
        ours.close()
        stream.close()
    finally:
        ours.close()
        peer.close()


def test_close_wakes_a_read_blocked_in_another_thread() -> None:
    """Closing the stream ends a read blocked in another thread at once, while the
    peer is still connected and silent. Without the shutdown the read waits for
    the peer on Linux and for the read timeout on macOS."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(5.0)
    port = listener.getsockname()[1]
    release_peer = threading.Event()

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            release_peer.wait(timeout=5.0)

    server = threading.Thread(target=serve)
    server.start()
    stream = _ShutdownBackend(httpcore2.SyncBackend()).connect_tcp("127.0.0.1", port, timeout=5.0)
    outcome: list[tuple[str, float]] = []

    def read() -> None:
        started = time.monotonic()
        try:
            data = stream.read(1, timeout=5.0)
        except httpcore2.ReadError:
            outcome.append(("read_error", time.monotonic() - started))
        else:
            outcome.append(("eof" if data == b"" else "data", time.monotonic() - started))

    reader = threading.Thread(target=read)
    reader.start()
    try:
        time.sleep(0.2)  # let the reader enter the blocking socket read
        stream.close()
        reader.join(timeout=5.0)
    finally:
        release_peer.set()
        reader.join(timeout=5.0)
        server.join(timeout=5.0)
        listener.close()
    assert outcome, "the read never returned"
    ((kind, elapsed),) = outcome
    # The kernel reports the shutdown either as end-of-stream or as an error on
    # the closed descriptor, depending on which call the reader was in.
    assert kind in {"read_error", "eof"}
    assert elapsed < 2.0


class _RecordedStream(httpcore2.NetworkStream):
    def __init__(self) -> None:
        self.timeouts: list[float | None] = []
        self.closed = False

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.timeouts.append(timeout)
        return b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.timeouts.append(timeout)

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.NetworkStream:
        self.timeouts.append(timeout)
        return self

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, info: str) -> Any:
        return None


def test_deadline_budget_decreases_across_read_write_and_tls() -> None:
    inner = _RecordedStream()
    stream = _ShutdownStream(inner)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with http_request_deadline(time.monotonic() + 1.0):
        stream.read(1, timeout=5.0)
        time.sleep(0.02)
        stream.write(b"request", timeout=None)
        time.sleep(0.02)
        stream.start_tls(context, timeout=5.0)
        stream.read(1, timeout=0.001)
    read_timeout, write_timeout, tls_timeout, shorter_timeout = inner.timeouts
    assert read_timeout is not None and write_timeout is not None and tls_timeout is not None
    assert 0 < tls_timeout < write_timeout < read_timeout <= 1.0
    assert read_timeout - tls_timeout >= 0.03
    assert shorter_timeout == 0.001


def test_deadline_context_restores_after_errors_and_is_thread_local() -> None:
    inner = _RecordedStream()
    stream = _ShutdownStream(inner)
    with http_request_deadline(time.monotonic() + 5.0):
        with (
            pytest.raises(ValueError, match="request failed"),
            http_request_deadline(time.monotonic() + 1.0),
        ):
            stream.read(1, timeout=10.0)
            raise ValueError("request failed")
        stream.read(1, timeout=10.0)
        other_thread = threading.Thread(target=stream.read, args=(1, 10.0))
        other_thread.start()
        other_thread.join(timeout=2.0)
        assert not other_thread.is_alive()
    stream.read(1, timeout=None)
    nested, restored, unrelated, subsequent = inner.timeouts
    assert nested is not None and 0 < nested <= 1.0
    assert restored is not None and 1.0 < restored <= 5.0
    assert unrelated == 10.0
    assert subsequent is None


def test_deadline_shutdown_wakes_a_read_even_without_a_socket_timeout() -> None:
    class BlockingRead(_HeldRead):
        def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            # Ignore the supplied timeout so only shutdown can end this read promptly.
            self._sock.settimeout(5.0)
            return self._sock.recv(max_bytes)

    ours, peer = socket.socketpair()
    inner = BlockingRead(ours)
    started = time.monotonic()
    try:
        with (
            pytest.raises(httpcore2.ReadTimeout, match="deadline exceeded"),
            http_request_deadline(started + 0.1),
        ):
            _ShutdownStream(inner).read(1, timeout=5.0)
        assert time.monotonic() - started < 1.0
        assert inner.closed.is_set()
    finally:
        ours.close()
        peer.close()


def test_deadline_ends_an_upload_that_keeps_making_progress() -> None:
    ours, peer = socket.socketpair()
    ours.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    peer.settimeout(2.0)
    received: list[int] = []

    def drain_slowly() -> None:
        while chunk := peer.recv(1024):
            received.append(len(chunk))
            time.sleep(0.005)

    reader = threading.Thread(target=drain_slowly)
    reader.start()
    stream = _ShutdownStream(SyncStream(ours))
    started = time.monotonic()
    try:
        with (
            pytest.raises(httpcore2.WriteTimeout, match="deadline exceeded"),
            http_request_deadline(started + 0.1),
        ):
            stream.write(b"x" * (256 * 1024), timeout=2.0)
        assert time.monotonic() - started < 0.75
        assert len(received) > 2, "the peer must accept data repeatedly before expiry"
    finally:
        stream.close()
        reader.join(timeout=3.0)
        peer.close()
    assert not reader.is_alive()


def test_expired_deadline_does_not_close_a_connection_reused_by_another_request() -> None:
    ours, peer = socket.socketpair()
    stream = _ShutdownStream(SyncStream(ours))
    expired = threading.Event()
    first = HTTPDeadline(time.monotonic() + 0.1)
    first.on_expire(expired.set)
    result: list[bytes | Exception] = []

    def next_request() -> None:
        try:
            with http_request_deadline(time.monotonic() + 2.0):
                result.append(stream.read(1, timeout=2.0))
        except Exception as exc:
            result.append(exc)

    reader = threading.Thread(target=next_request)
    try:
        peer.sendall(b"a")
        with first.activate():
            assert stream.read(1, timeout=1.0) == b"a"
        reader.start()
        assert expired.wait(timeout=1.0)
        first.close()
        peer.sendall(b"b")
        reader.join(timeout=3.0)
        assert result == [b"b"]
    finally:
        first.close()
        stream.close()
        if reader.ident is not None:
            reader.join(timeout=3.0)
        peer.close()


def test_closing_deadline_waits_for_an_in_progress_expiry_callback() -> None:
    callback_started = threading.Event()
    release_callback = threading.Event()
    close_finished = threading.Event()

    def on_expire() -> None:
        callback_started.set()
        release_callback.wait(timeout=5.0)

    budget = HTTPDeadline(time.monotonic() + 0.05)
    budget.on_expire(on_expire)

    def close() -> None:
        budget.close()
        close_finished.set()

    closer = threading.Thread(target=close)
    try:
        assert callback_started.wait(timeout=1.0)
        closer.start()
        assert not close_finished.wait(timeout=0.05)
        release_callback.set()
        assert close_finished.wait(timeout=1.0)
    finally:
        release_callback.set()
        budget.close()
        if closer.ident is not None:
            closer.join(timeout=2.0)


def test_tls_result_is_closed_if_the_deadline_expires_during_the_handshake() -> None:
    expired = threading.Event()
    secured = _RecordedStream()

    class DelayedTLS(_RecordedStream):
        def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> httpcore2.NetworkStream:
            assert expired.wait(timeout=1.0)
            return secured

    inner = DelayedTLS()
    budget = HTTPDeadline(time.monotonic() + 0.05)
    budget.on_expire(expired.set)
    try:
        with pytest.raises(httpcore2.ConnectTimeout, match="deadline exceeded"), budget.activate():
            _ShutdownStream(inner).start_tls(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        assert secured.closed
        assert inner.closed
    finally:
        budget.close()


def test_slow_dns_releases_caller_without_connecting_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    resolvers: list[threading.Thread] = []
    socket_calls: list[tuple[Any, ...]] = []

    def resolve(*args: Any, **kwargs: Any) -> list[Any]:
        resolvers.append(threading.current_thread())
        assert release.wait(timeout=2.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))]

    def unexpected_socket(*args: Any, **kwargs: Any) -> Any:
        socket_calls.append(args)
        raise AssertionError("expired DNS must not start a connection")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", unexpected_socket)
    started = time.monotonic()
    try:
        with pytest.raises(httpcore2.ConnectTimeout), http_request_deadline(started + 0.1):
            _ShutdownBackend(httpcore2.SyncBackend()).connect_tcp("slow.invalid", 80, timeout=1.0)
        assert time.monotonic() - started < 0.8
        assert resolvers and resolvers[0] is not threading.current_thread()
        assert resolvers[0].daemon
        assert resolvers[0].is_alive(), "the caller stopped waiting for an unfinished lookup"
    finally:
        release.set()
        for resolver in resolvers:
            if resolver is not threading.current_thread():
                resolver.join(timeout=2.0)
                assert not resolver.is_alive()
    assert not socket_calls


def test_expired_dns_work_keeps_its_capacity_until_it_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    resolvers: list[threading.Thread] = []
    answer = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))]
    slots = threading.BoundedSemaphore(2)

    def resolve(*args: Any, **kwargs: Any) -> list[Any]:
        resolvers.append(threading.current_thread())
        assert release.wait(timeout=2.0)
        return answer

    monkeypatch.setattr(provider_http, "_DNS_SLOTS", slots)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    try:
        for _ in range(3):
            started = time.monotonic()
            with pytest.raises(httpcore2.ConnectTimeout):
                provider_http._resolve_tcp("slow.invalid", 80, started + 0.05)
            assert time.monotonic() - started < 0.8
        assert len(resolvers) == 2, "a full resolver budget must not spawn or queue more work"
        assert all(resolver.daemon and resolver.is_alive() for resolver in resolvers)
    finally:
        release.set()
        for resolver in resolvers:
            resolver.join(timeout=2.0)
            assert not resolver.is_alive()
    assert provider_http._resolve_tcp("recovered.invalid", 80, time.monotonic() + 1.0) == answer
    for resolver in resolvers:
        resolver.join(timeout=2.0)
        assert not resolver.is_alive()
    assert len(resolvers) == 3


def test_numeric_hosts_connect_while_dns_capacity_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)  # Unfinished lookups hold every slot.
    monkeypatch.setattr(provider_http, "_DNS_SLOTS", slots)
    lookups: list[tuple[str, threading.Thread, int]] = []
    original_getaddrinfo = socket.getaddrinfo

    def recorded(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        lookups.append((host, threading.current_thread(), kwargs.get("flags", 0)))
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", recorded)
    listener = socket.create_server(("127.0.0.1", 0))
    try:
        # Waiting for a slot would time out within this budget, failing the connect.
        with http_request_deadline(time.monotonic() + 2.0):
            stream = _ShutdownBackend(httpcore2.SyncBackend()).connect_tcp(
                "127.0.0.1", listener.getsockname()[1], timeout=2.0, local_address="127.0.0.1"
            )
        stream.close()
        # A name still needs a slot, so the exhausted capacity stays enforced.
        with pytest.raises(httpcore2.ConnectTimeout, match="during DNS"):
            provider_http._resolve_tcp("named.invalid", 80, time.monotonic() + 0.05)
    finally:
        listener.close()
        slots.release()
    caller = threading.current_thread()
    assert [(host, thread) for host, thread, _ in lookups] == [("127.0.0.1", caller)] * 2
    assert all(flags & socket.AI_NUMERICHOST for *_, flags in lookups)


class _ConnectSocket:
    """A socket-shaped recorder for deterministic address-iteration tests."""

    def __init__(self, family: int, kind: int, protocol: int = 0) -> None:
        self.family = family
        self.kind = kind
        self.protocol = protocol
        self.timeout: float | None = None
        self.bound: Any = None
        self.connected: Any = None
        self.options: list[tuple[Any, ...]] = []
        self.closed = False

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def bind(self, address: Any) -> None:
        self.bound = address

    def connect(self, address: Any) -> None:
        self.connected = address

    def setsockopt(self, *option: Any) -> None:
        self.options.append(option)

    def shutdown(self, how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_address_attempts_share_the_smaller_connect_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    sockets: list[_ConnectSocket] = []
    attempts: list[float] = []

    class UnreachableSocket(_ConnectSocket):
        def __init__(self, *args: Any) -> None:
            super().__init__(*args)
            sockets.append(self)

        def connect(self, address: Any) -> None:
            assert self.timeout is not None
            attempts.append(self.timeout)
            now[0] += min(4.0, self.timeout)
            raise TimeoutError("unreachable address")

    monkeypatch.setattr(provider_http, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(socket, "socket", UnreachableSocket)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (f"192.0.2.{i}", 80))
            for i in range(1, 5)
        ],
    )
    with pytest.raises(httpcore2.ConnectTimeout), http_request_deadline(120.0):
        _ShutdownBackend(httpcore2.SyncBackend()).connect_tcp("many.invalid", 80, timeout=10.0)
    assert attempts == [10.0, 6.0, 2.0]
    assert now[0] == 110.0
    assert len(sockets) == 3
    assert all(sock.closed for sock in sockets)


@pytest.mark.parametrize("failure", [None, "connect", "option"])
def test_deadline_connect_preserves_addresses_options_and_socket_cleanup(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    sockets: list[_ConnectSocket] = []
    destination = ("fe80::1", 8443, 0, 7)
    source = ("fe80::2", 0, 0, 7)
    custom_option = (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    class RecordedSocket(_ConnectSocket):
        def __init__(self, *args: Any) -> None:
            super().__init__(*args)
            sockets.append(self)

        def connect(self, address: Any) -> None:
            super().connect(address)
            if failure == "connect":
                raise OSError("connection refused")

        def setsockopt(self, *option: Any) -> None:
            super().setsockopt(*option)
            if failure == "option":
                raise OSError("invalid option")

    def resolve(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        address = destination if host == "backend.invalid" else source
        return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)]

    monkeypatch.setattr(socket, "socket", RecordedSocket)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    backend = _ShutdownBackend(httpcore2.SyncBackend())
    with http_request_deadline(time.monotonic() + 5.0):
        if failure is not None:
            with pytest.raises(httpcore2.ConnectError):
                backend.connect_tcp("backend.invalid", 8443, 2.0, "source.invalid", [custom_option])
        else:
            stream = backend.connect_tcp(
                "backend.invalid", 8443, 2.0, "source.invalid", [custom_option]
            )
            assert isinstance(stream, _ShutdownStream)
            assert not sockets[0].closed
            stream.close()
    [sock] = sockets
    assert (sock.family, sock.kind, sock.protocol) == (
        socket.AF_INET6,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
    )
    assert sock.bound == source
    if failure != "option":
        assert sock.connected == destination
    if failure is None:
        assert custom_option in sock.options
        assert (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) in sock.options
    assert sock.closed


def test_deadline_connect_tries_next_address_when_socket_family_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    families: list[int] = []
    sockets: list[_ConnectSocket] = []

    class IPv4OnlySocket(_ConnectSocket):
        def __init__(self, family: int, kind: int, protocol: int = 0) -> None:
            families.append(family)
            if family == socket.AF_INET6:
                raise OSError("IPv6 is unavailable")
            super().__init__(family, kind, protocol)
            sockets.append(self)

    monkeypatch.setattr(socket, "socket", IPv4OnlySocket)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 80, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80)),
        ],
    )
    with http_request_deadline(time.monotonic() + 5.0):
        stream = _ShutdownBackend(httpcore2.SyncBackend()).connect_tcp("dual.invalid", 80, 2.0)
        try:
            assert families == [socket.AF_INET6, socket.AF_INET]
            [sock] = sockets
            assert sock.connected == ("127.0.0.1", 80)
            assert not sock.closed
        finally:
            stream.close()
    assert sockets[0].closed


@pytest.mark.parametrize("custom_backend", [False, True])
def test_deadline_connect_keeps_unbudgeted_and_custom_backend_delegation(
    monkeypatch: pytest.MonkeyPatch, custom_backend: bool
) -> None:
    calls: list[tuple[Any, ...]] = []
    inner_stream = _RecordedStream()

    class CustomBackend(httpcore2.SyncBackend):
        pass

    def connect(self: Any, host: str, port: int, **kwargs: Any) -> httpcore2.NetworkStream:
        calls.append((host, port, kwargs))
        return inner_stream

    monkeypatch.setattr(httpcore2.SyncBackend, "connect_tcp", connect)
    inner = CustomBackend() if custom_backend else httpcore2.SyncBackend()
    option = (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    deadline = time.monotonic() + 5.0 if custom_backend else None
    with http_request_deadline(deadline):
        wrapped = _ShutdownBackend(inner).connect_tcp(
            "backend.invalid", 80, 2.0, "source.invalid", [option]
        )
    assert isinstance(wrapped, _ShutdownStream)
    assert wrapped._inner is inner_stream
    assert calls == [
        (
            "backend.invalid",
            80,
            {"timeout": 2.0, "local_address": "source.invalid", "socket_options": [option]},
        )
    ]
