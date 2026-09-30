"""HTTP transport that connects only to the addresses screened for one hop."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from ipaddress import ip_address
from time import monotonic
from urllib.request import getproxies

import httpcore2
import httpx2

from turnstone.core.ip_classify import IPAddress

_HTTP_ERRORS: dict[type[Exception], type[httpx2.TransportError]] = {
    httpcore2.ConnectTimeout: httpx2.ConnectTimeout,
    httpcore2.ReadTimeout: httpx2.ReadTimeout,
    httpcore2.WriteTimeout: httpx2.WriteTimeout,
    httpcore2.PoolTimeout: httpx2.PoolTimeout,
    httpcore2.ConnectError: httpx2.ConnectError,
    httpcore2.ReadError: httpx2.ReadError,
    httpcore2.WriteError: httpx2.WriteError,
    httpcore2.LocalProtocolError: httpx2.LocalProtocolError,
    httpcore2.RemoteProtocolError: httpx2.RemoteProtocolError,
    httpcore2.ProxyError: httpx2.ProxyError,
    httpcore2.UnsupportedProtocol: httpx2.UnsupportedProtocol,
    httpcore2.TimeoutException: httpx2.TimeoutException,
    httpcore2.NetworkError: httpx2.NetworkError,
    httpcore2.ProtocolError: httpx2.ProtocolError,
}


def proxy_required(url: httpx2.URL) -> bool:
    """Check proxy settings without resolving a name or creating a transport.

    Match the client's exclusions: a plain domain includes its subdomains,
    a leading dot excludes only subdomains, and IPs and localhost are exact.
    URL-form exclusions can also restrict the scheme and port.
    """
    proxies = getproxies()
    if not (proxies.get(url.scheme) or proxies.get("all")):
        return False
    for entry in proxies.get("no", "").split(","):
        entry = entry.strip()
        if entry == "*":
            return False
        if not entry:
            continue
        if "://" not in entry:
            try:
                address = ip_address(entry.split("/")[0])
            except ValueError:
                entry = "all://" + (entry if entry.lower() == "localhost" else "*" + entry)
            else:
                entry = "all://" + (f"[{entry}]" if address.version == 6 else entry)
        try:
            exclusion = httpx2.URL(entry)
        except httpx2.InvalidURL:
            continue
        if exclusion.scheme not in ("", "all", url.scheme):
            continue
        if exclusion.port is not None and exclusion.port != url.port:
            continue
        host = exclusion.host
        if not host or host == "*":
            return False
        if host.startswith("*."):
            if url.host.endswith(host[1:]):
                return False
        elif host.startswith("*"):
            if url.host == host[1:] or url.host.endswith("." + host[1:]):
                return False
        elif url.host == host:
            return False
    return True


@contextmanager
def _map_errors() -> Iterator[None]:
    """Keep the exception contract used by guarded-fetch callers."""
    try:
        yield
    except Exception as exc:
        for core_type, http_type in _HTTP_ERRORS.items():
            if isinstance(exc, core_type):
                raise http_type(str(exc)) from exc
        raise


class _PinnedBackend(httpcore2.NetworkBackend):
    def __init__(self, hostname: str, port: int, addresses: tuple[IPAddress, ...]) -> None:
        self._hostname = hostname
        self._port = port
        self._addresses = addresses
        self._backend = httpcore2.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        if host != self._hostname or port != self._port:
            raise httpcore2.ConnectError("Connection target has no matching address screen")
        deadline = None if timeout is None else monotonic() + timeout
        error: httpcore2.ConnectError | httpcore2.ConnectTimeout = httpcore2.ConnectError(
            "No screened addresses are available"
        )
        for address in self._addresses:
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                raise httpcore2.ConnectTimeout(
                    "Timed out connecting to screened addresses"
                ) from error
            target = str(address)
            # Classification drops a numeric literal's IPv6 scope. Retain its
            # interface selection when dialing the screened numeric address.
            if address.version == 6 and ":" in host and "%" in host:
                target += "%" + host.partition("%")[2]
            try:
                return self._backend.connect_tcp(
                    target,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as exc:
                error = exc
        raise error


class _ResponseStream(httpx2.SyncByteStream):
    def __init__(self, response: httpcore2.Response) -> None:
        self._response = response

    def __iter__(self) -> Iterator[bytes]:
        with _map_errors():
            yield from self._response.iter_stream()

    def close(self) -> None:
        with _map_errors():
            self._response.close()


class PinnedTransport(httpx2.BaseTransport):
    """Keep HTTP/TLS identity on the URL and pin only the socket destination.

    One fetch owns this transport and updates its target between redirect
    hops, after the prior response is closed. Each hop gets a fresh pool so
    an older connection cannot bypass its current address screen. The client
    retains its cookie jar across hops.

    Certificates are verified against the operating system's trust store.
    ``SSL_CERT_FILE`` or ``SSL_CERT_DIR`` replaces it with a custom CA bundle.
    """

    def __init__(self) -> None:
        # One context per fetch, as before the move to httpx2. With the OS trust
        # store it costs well under a millisecond (an SSL_CERT_FILE bundle costs
        # more, as certifi's did), and truststore reloads the store on every
        # handshake whether or not a context is shared.
        self._ssl_context = httpx2.create_ssl_context()
        self._pool: httpcore2.ConnectionPool | None = None
        self._url: httpx2.URL | None = None

    def pin(self, url: str, hostname: str, addresses: tuple[IPAddress, ...]) -> None:
        parsed = httpx2.URL(url)
        if not addresses or hostname != parsed.raw_host.decode("ascii"):
            raise ValueError("Blocked: URL has no matching screened addresses")
        self.close()
        self._url = parsed
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._pool = httpcore2.ConnectionPool(
            ssl_context=self._ssl_context,
            network_backend=_PinnedBackend(hostname, port, addresses),
        )

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        if self._pool is None or request.url != self._url:
            raise httpx2.ConnectError("Request has no matching address screen")
        assert isinstance(request.stream, httpx2.SyncByteStream)
        core_request = httpcore2.Request(
            method=request.method,
            url=httpcore2.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_errors():
            response = self._pool.handle_request(core_request)
        return httpx2.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response),
            extensions=response.extensions,
        )

    def close(self) -> None:
        if self._pool is not None:
            with _map_errors():
                self._pool.close()
            self._pool = None
        self._url = None
