"""Turnstone's SDK HTTP clients wake a blocked read when closed from another thread."""

from __future__ import annotations

import socket
import threading
import time

import anthropic
import httpcore2
import httpx2
import openai
import pytest

from turnstone.core.providers._http_client import _ShutdownBackend, shutdown_on_close


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


def test_shutdown_on_close_refuses_an_unknown_transport() -> None:
    client = httpx2.Client(transport=httpx2.MockTransport(lambda request: httpx2.Response(200)))
    with pytest.raises(TypeError, match="HTTPTransport"):
        shutdown_on_close(client)


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
