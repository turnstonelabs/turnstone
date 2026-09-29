"""Exercise guarded fetches through the HTTP stack with the network mocked."""

import gzip
import ipaddress
import socket
import ssl
import sys
import tracemalloc
import zlib

import httpcore2
import httpx2
import pytest

from turnstone.core.web import UrlBlockedError, fetch_with_ssrf_guard

_CRLF = bytes([13, 10])
_PUBLIC = "93.184.216.34"
_OTHER_PUBLIC = "93.184.216.35"
_PUBLIC_V6 = "2606:4700:4700::1111"


def _response(status=200, headers=(), body=b"ok"):
    lines = [f"HTTP/1.1 {status} Test".encode(), f"Content-Length: {len(body)}".encode()]
    lines.extend(f"{name}: {value}".encode() for name, value in headers)
    return _CRLF.join([*lines, b"", body])


def _request_headers(stream):
    """Parse the header block of the one request written to *stream*."""
    head = bytes(stream.written).split(_CRLF * 2, 1)[0]
    fields = (line.partition(b":") for line in head.split(_CRLF)[1:])
    return {name.decode().lower(): value.decode().strip() for name, _sep, value in fields}


class _Stream(httpcore2.MockStream):
    def __init__(self, responses):
        super().__init__(responses)
        self.written = bytearray()
        self.tls = []

    def write(self, buffer, timeout=None):
        self.written.extend(buffer)

    def read(self, max_bytes, timeout=None):
        if self._buffer and isinstance(self._buffer[0], Exception):
            raise self._buffer.pop(0)
        return super().read(max_bytes, timeout)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((server_hostname, ssl_context.check_hostname, ssl_context.verify_mode))
        return self


class _Network:
    """Script DNS answers and connections, while retaining the real HTTP client."""

    def __init__(self, monkeypatch, answers, responses):
        self.answers = answers
        self.responses = list(responses)
        self.lookups = []
        self.attempts = []
        self.streams = []
        self.failures = {}
        real_resolve = socket.getaddrinfo

        def resolve(host, port=None, *args, **kwargs):
            if kwargs.get("flags", 0) & socket.AI_NUMERICHOST:
                return real_resolve(host, port, *args, **kwargs)
            try:
                ipaddress.ip_address(host)
            except ValueError:
                self.lookups.append(host)
                if host not in self.answers:
                    raise socket.gaierror(socket.EAI_NONAME, "unknown test hostname") from None
                batches = self.answers[host]
                addresses = batches.pop(0) if len(batches) > 1 else batches[0]
            else:
                addresses = [host]
            return [
                (
                    socket.AF_INET6 if ":" in address else socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, port or 0, 0, 0) if ":" in address else (address, port or 0),
                )
                for address in addresses
            ]

        def connect(_backend, host, port, timeout=None, **kwargs):
            # Match the default connector's resolution boundary. Numeric hosts
            # are handled locally; a hostname would cause another DNS query.
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            address = infos[0][4][0]
            self.attempts.append((host, address, port, timeout))
            if address in self.failures:
                raise self.failures[address]
            assert self.responses, "unexpected connection"
            stream = _Stream(self.responses.pop(0))
            self.streams.append(stream)
            return stream

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(httpcore2.SyncBackend, "connect_tcp", connect)


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("unexpected live network call")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://service.example/x",
        "https://service.example:8443/x",
        "https://straße.example/x",
        "http://service.example",
        "http://service.example:0/x",
        "http://user:secret@service.example/x#part",
    ],
)
def test_screened_address_is_used_without_changing_the_hostname(monkeypatch, url):
    parsed = httpx2.URL(url)
    hostname = parsed.raw_host.decode("ascii")
    net = _Network(monkeypatch, {hostname: [[_PUBLIC], ["10.0.0.5"]]}, [[_response()]])

    response = fetch_with_ssrf_guard(url, timeout=5)

    assert response.content == b"ok"
    assert response.url == parsed
    assert net.lookups == [hostname]
    assert [(host, addr) for host, addr, _port, _timeout in net.attempts] == [(_PUBLIC, _PUBLIC)]
    assert b"Host: " + parsed.netloc in bytes(net.streams[0].written).split(_CRLF)
    expected_tls = [(hostname, True, ssl.CERT_REQUIRED)] if parsed.scheme == "https" else []
    assert net.streams[0].tls == expected_tls
    assert net.streams[0]._closed


@pytest.mark.parametrize("tool", ["web_fetch", "open_preview"])
def test_private_grant_passes_its_resolved_addresses_to_the_connection(monkeypatch, tmp_db, tool):
    from tests.test_open_preview_tool import _make_session, _prepare_url_tool
    from turnstone.core.session import ChatSession

    host = "lan.example"
    net = _Network(monkeypatch, {host: [["10.0.0.5"]]}, [[_response(404)]])
    monkeypatch.setattr(ChatSession, "_allow_private_network", lambda self: True)
    session = _make_session()
    first = _prepare_url_tool(session, tool, f"http://{host}/x")
    first["execute"](first)
    assert net.attempts == []
    second = _prepare_url_tool(session, tool, f"http://{host}/x")
    assert second["allow_private_origin"]

    _call_id, output = second["execute"](second)

    assert output == "Error: fetch failed: HTTP 404"
    assert net.lookups == [host, host]
    assert [attempt[0] for attempt in net.attempts] == ["10.0.0.5"]


@pytest.mark.parametrize("tool", ["web_fetch", "open_preview"])
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("status", "Error: fetch failed: HTTP 503"),
        ("connect", "Error: fetch failed: connection refused"),
        ("read", "Error: fetch failed: body stalled"),
    ],
    ids=["status", "connect", "read"],
)
def test_http_failures_become_fetch_errors_in_both_tools(monkeypatch, tool, failure, expected):
    from tests.test_open_preview_tool import _make_session, _prepare_url_tool

    scripts = {
        "status": [[_response(503)]],
        "connect": [],
        "read": [
            [
                _CRLF.join([b"HTTP/1.1 200 OK", b"Content-Length: 5", b"", b"a"]),
                httpcore2.ReadTimeout("body stalled"),
            ]
        ],
    }
    net = _Network(monkeypatch, {"service.example": [[_PUBLIC]]}, scripts[failure])
    if failure == "connect":
        net.failures[_PUBLIC] = httpcore2.ConnectError("connection refused")
    item = _prepare_url_tool(_make_session(), tool, "http://service.example/x")

    _call_id, output = item["execute"](item)

    assert output == expected
    assert [attempt[0] for attempt in net.attempts] == [_PUBLIC]


@pytest.mark.parametrize(
    ("content_type", "body", "kind"),
    [
        ("application/pdf", b"%PDF-1.4 preview", "pdf"),
        ("image/png", None, "image"),
        ("application/json", b'[{"a": 1, "b": 2}]', "table"),
        ("text/csv", b"a,b\n1,2\n", "table"),
    ],
)
def test_url_previews_classify_fetched_content(monkeypatch, content_type, body, kind):
    from tests.test_open_preview_tool import PNG_1x1, _make_session, _prepare_url_tool

    body = PNG_1x1 if body is None else body
    _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC]]},
        [[_response(headers=[("Content-Type", content_type)], body=body)]],
    )
    session = _make_session()
    item = _prepare_url_tool(session, "open_preview", "https://user:pw@service.example/data")

    _call_id, output = item["execute"](item)

    assert not output.startswith("Error"), output
    descriptor, attachment = session._tool_previews["c1"]
    assert descriptor["kind"] == kind
    assert descriptor["source"] == "https://service.example/data"
    assert attachment.content == body


def test_redirect_with_a_changed_address_reconnects_and_preserves_cookies(monkeypatch):
    net = _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC], [_OTHER_PUBLIC]]},
        [
            [_response(302, [("Location", "/next"), ("Set-Cookie", "route=ok; Path=/")])],
            [_response()],
        ],
    )

    response = fetch_with_ssrf_guard("https://service.example/start", timeout=5)

    assert str(response.url) == "https://service.example/next"
    assert net.lookups == ["service.example", "service.example"]
    assert [attempt[0] for attempt in net.attempts] == [_PUBLIC, _OTHER_PUBLIC]
    assert b"Cookie: route=ok" in bytes(net.streams[1].written).split(_CRLF)
    assert all(stream._closed for stream in net.streams)


def test_redirect_to_the_same_host_is_screened_again(monkeypatch):
    net = _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC]]},
        [[_response(302, [("Location", "/next")])], [_response()]],
    )

    response = fetch_with_ssrf_guard("https://service.example/start", timeout=5)

    assert response.content == b"ok"
    assert net.lookups == ["service.example", "service.example"]
    assert [attempt[0] for attempt in net.attempts] == [_PUBLIC, _PUBLIC]
    assert all(stream._closed for stream in net.streams)


@pytest.mark.parametrize("target", ["10.0.0.5", "169.254.169.254"])
def test_redirect_to_a_blocked_address_never_connects(monkeypatch, target):
    net = _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC]], "target.example": [[target]]},
        [[_response(302, [("Location", "http://target.example/x")])]],
    )

    with pytest.raises(UrlBlockedError) as error:
        fetch_with_ssrf_guard("http://service.example/start", timeout=5)

    assert error.value.hop == 1
    assert net.lookups == ["service.example", "target.example"]
    assert [attempt[0] for attempt in net.attempts] == [_PUBLIC]
    assert net.streams[0]._closed


def test_connection_fallback_uses_only_the_screened_addresses(monkeypatch):
    net = _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC_V6, _PUBLIC], ["10.0.0.5"]]},
        [[_response()]],
    )
    net.failures[_PUBLIC_V6] = httpcore2.ConnectError("IPv6 unavailable")

    response = fetch_with_ssrf_guard("https://service.example/x", timeout=5)

    assert response.content == b"ok"
    assert net.lookups == ["service.example"]
    assert [attempt[0] for attempt in net.attempts] == [_PUBLIC_V6, _PUBLIC]


def test_exhausted_addresses_fail_without_another_hostname_lookup(monkeypatch):
    net = _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC, _OTHER_PUBLIC], ["10.0.0.5"]]},
        [],
    )
    net.failures = {
        _PUBLIC: httpcore2.ConnectError("first unavailable"),
        _OTHER_PUBLIC: httpcore2.ConnectError("second unavailable"),
    }

    with pytest.raises(httpx2.ConnectError, match="second unavailable"):
        fetch_with_ssrf_guard("http://service.example/x", timeout=5)

    assert net.lookups == ["service.example"]
    assert [attempt[0] for attempt in net.attempts] == [_PUBLIC, _OTHER_PUBLIC]


def test_stream_errors_keep_the_http_exception_type(monkeypatch):
    net = _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC]]},
        [
            [
                _CRLF.join([b"HTTP/1.1 200 OK", b"Content-Length: 5", b"", b"a"]),
                httpcore2.ReadTimeout("body stalled"),
            ]
        ],
    )

    with pytest.raises(httpx2.ReadTimeout, match="body stalled"):
        fetch_with_ssrf_guard("http://service.example/x", timeout=5)

    assert net.streams[0]._closed


@pytest.mark.parametrize("proxy_var", ["HTTP_PROXY", "http_proxy", "ALL_PROXY"])
def test_proxy_requests_are_refused_before_dns(monkeypatch, proxy_var):
    net = _Network(monkeypatch, {"service.example": [[_PUBLIC]]}, [])
    monkeypatch.setenv(proxy_var, "http://proxy.example:8080")

    with pytest.raises(ValueError, match="proxy"):
        fetch_with_ssrf_guard("http://service.example/x", timeout=5)

    assert net.lookups == []
    assert net.attempts == []


@pytest.mark.parametrize("no_proxy", ["service.example", "*"])
def test_no_proxy_targets_can_connect_directly(monkeypatch, no_proxy):
    net = _Network(monkeypatch, {"service.example": [[_PUBLIC]]}, [[_response()]])
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", no_proxy)

    response = fetch_with_ssrf_guard("https://service.example/x", timeout=5)

    assert response.content == b"ok"
    assert net.lookups == ["service.example"]
    assert [attempt[0] for attempt in net.attempts] == [_PUBLIC]


@pytest.mark.parametrize(
    ("no_proxy", "url", "bypass"),
    [
        (".service.example", "https://service.example/x", False),
        (".service.example", "https://sub.service.example/x", True),
        ("service.example", "https://sub.service.example/x", True),
        ("service.example", "https://otherservice.example/x", False),
        ("service.example:8443", "https://service.example/x", False),
        ("service.example:8443", "https://service.example:8443/x", True),
        ("https://service.example", "https://service.example/x", True),
        ("https://service.example", "http://service.example/x", False),
        ("https://service.example", "https://sub.service.example/x", False),
        ("localhost", "http://sub.localhost/x", False),
        ("::1", "http://[::1]/x", True),
    ],
)
def test_proxy_exclusions_match_the_url(monkeypatch, no_proxy, url, bypass):
    hostname = httpx2.URL(url).raw_host.decode("ascii")
    net = _Network(monkeypatch, {hostname: [[_PUBLIC]]}, [[_response()]])
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", no_proxy)

    if bypass:
        response = fetch_with_ssrf_guard(url, timeout=5, allow_private_origin=True)
        assert response.content == b"ok"
        assert len(net.attempts) == 1
    else:
        with pytest.raises(ValueError, match="proxy"):
            fetch_with_ssrf_guard(url, timeout=5, allow_private_origin=True)
        assert net.lookups == []
        assert net.attempts == []


@pytest.mark.parametrize("url", ["http://[::1", "http://service.example:bad/x"])
def test_malformed_urls_keep_the_blocked_error_type(monkeypatch, url):
    net = _Network(monkeypatch, {}, [])

    with pytest.raises(UrlBlockedError, match="malformed"):
        fetch_with_ssrf_guard(url, timeout=5)

    assert net.lookups == []
    assert net.attempts == []


def _compressor(coding):
    if coding == "gzip":
        return gzip.compress
    if coding == "deflate":
        return zlib.compress
    if coding == "zstd":
        module = "compression.zstd" if sys.version_info >= (3, 14) else "backports.zstd"
        return pytest.importorskip(module).compress
    return pytest.importorskip("brotli").compress


@pytest.mark.parametrize("coding", ["gzip", "deflate", "zstd", "br"])
def test_advertised_encodings_obey_the_decoded_byte_limit(monkeypatch, coding):
    compress = _compressor(coding)
    net = _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC]]},
        [
            [_response(headers=[("Content-Encoding", coding)], body=compress(b"a" * 100))],
            [_response(headers=[("Content-Encoding", coding)], body=compress(b"a" * 101))],
        ],
    )

    response = fetch_with_ssrf_guard("http://service.example/x", timeout=5, max_bytes=100)
    advertised = _request_headers(net.streams[0])["accept-encoding"]
    if coding not in advertised.split(", "):
        pytest.skip(f"{coding} is not advertised here ({advertised})")
    with pytest.raises(ValueError, match="exceeded the 100-byte fetch limit"):
        fetch_with_ssrf_guard("http://service.example/x", timeout=5, max_bytes=100)

    assert response.content == b"a" * 100
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == "100"
    assert net.streams[0]._closed
    assert net.streams[1]._closed


def test_compressed_body_is_decoded_in_bounded_pieces(monkeypatch):
    compressor = zlib.compressobj(wbits=31)
    zeros = bytes(1024 * 1024)
    body = b"".join(compressor.compress(zeros) for _ in range(64)) + compressor.flush()
    _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC]]},
        [[_response(headers=[("Content-Encoding", "gzip")], body=body)]],
    )

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    before, _peak = tracemalloc.get_traced_memory()
    try:
        with pytest.raises(ValueError, match="fetch limit"):
            fetch_with_ssrf_guard("http://service.example/x", timeout=5, max_bytes=1024 * 1024)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()

    assert peak - before < 16 * 1024 * 1024


def test_fallback_shares_one_connect_timeout(monkeypatch):
    from turnstone.core import _web_transport

    net = _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC, _OTHER_PUBLIC]]},
        [[_response()]],
    )
    net.failures[_PUBLIC] = httpcore2.ConnectError("unavailable")
    times = iter([100, 100, 103])
    monkeypatch.setattr(_web_transport, "monotonic", lambda: next(times))

    response = fetch_with_ssrf_guard("http://service.example/x", timeout=5)

    assert response.content == b"ok"
    assert net.attempts == [(_PUBLIC, _PUBLIC, 80, 5), (_OTHER_PUBLIC, _OTHER_PUBLIC, 80, 2)]


def test_expired_connect_timeout_does_not_try_another_address(monkeypatch):
    from turnstone.core import _web_transport

    net = _Network(monkeypatch, {"service.example": [[_PUBLIC, _OTHER_PUBLIC]]}, [])
    net.failures[_PUBLIC] = httpcore2.ConnectTimeout("unavailable")
    times = iter([100, 100, 106])
    monkeypatch.setattr(_web_transport, "monotonic", lambda: next(times))

    with pytest.raises(httpx2.ConnectTimeout, match="Timed out connecting"):
        fetch_with_ssrf_guard("http://service.example/x", timeout=5)

    assert net.attempts == [(_PUBLIC, _PUBLIC, 80, 5)]


def test_redirect_requiring_a_proxy_is_refused_before_its_lookup(monkeypatch):
    net = _Network(
        monkeypatch,
        {"service.example": [[_PUBLIC]]},
        [[_response(302, [("Location", "http://target.example/x")])]],
    )
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "service.example")

    with pytest.raises(ValueError, match="proxy"):
        fetch_with_ssrf_guard("http://service.example/start", timeout=5)

    assert net.lookups == ["service.example"]
    assert len(net.attempts) == 1
    assert net.streams[0]._closed


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]", "[::1%1]"])
def test_private_literals_connect_without_a_hostname_lookup(monkeypatch, host):
    net = _Network(monkeypatch, {}, [[_response()]])

    response = fetch_with_ssrf_guard(f"http://{host}/x", timeout=5, allow_private_origin=True)

    assert response.content == b"ok"
    assert net.lookups == []
    assert net.attempts[0][0] == host.strip("[]")


@pytest.mark.parametrize("hostname", ["", "other.example"])
def test_first_hop_screen_must_match_the_requested_hostname(monkeypatch, hostname):
    from turnstone.core.ip_classify import AddressLane
    from turnstone.core.web import UrlScreen

    net = _Network(monkeypatch, {}, [])
    screen = UrlScreen(AddressLane.PUBLIC, None, False, (ipaddress.ip_address(_PUBLIC),), hostname)

    with pytest.raises(ValueError, match="no matching screened addresses"):
        fetch_with_ssrf_guard("http://service.example/x", timeout=5, first_hop_screen=screen)

    assert net.lookups == []
    assert net.attempts == []
