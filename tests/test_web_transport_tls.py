"""Check guarded-fetch certificate verification against a loopback HTTPS server."""

import http.server
import socket
import ssl
import threading

import httpx2
import pytest

from turnstone.core.web import fetch_with_ssrf_guard

_HOST = "tls.example"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"secure"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def issue(tmp_path):
    """Mint one private CA; return a function writing its root and a leaf per hostname."""
    from lacme import CertificateAuthority, MemoryStore
    from lacme.mtls import write_pem_files

    ca = CertificateAuthority(store=MemoryStore())
    ca.init()

    def _issue(hostname):
        directory = tmp_path / hostname
        directory.mkdir()
        return write_pem_files(ca.issue([hostname]), ca_pem=ca.root_cert_pem, directory=directory)

    return _issue


@pytest.fixture
def serve(monkeypatch):
    """Serve HTTPS on loopback under the test hostname, with no ambient trust or proxy."""
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        return real_getaddrinfo("127.0.0.1" if host == _HOST else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    started = []

    def _serve(paths):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(paths.cert), str(paths.key))
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return f"https://{_HOST}:{server.server_address[1]}/x"

    yield _serve

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_configured_ca_bundle_is_trusted(monkeypatch, issue, serve):
    paths = issue(_HOST)
    url = serve(paths)
    monkeypatch.setenv("SSL_CERT_FILE", str(paths.ca))

    response = fetch_with_ssrf_guard(url, timeout=5, allow_private_origin=True)

    assert response.content == b"secure"


def test_certificate_outside_the_system_trust_store_is_rejected(issue, serve):
    url = serve(issue(_HOST))

    with pytest.raises(httpx2.ConnectError, match="(?i)certificate verify failed"):
        fetch_with_ssrf_guard(url, timeout=5, allow_private_origin=True)


def test_certificate_for_another_hostname_is_rejected(monkeypatch, issue, serve):
    paths = issue("other.example")
    url = serve(paths)
    monkeypatch.setenv("SSL_CERT_FILE", str(paths.ca))

    with pytest.raises(httpx2.ConnectError, match="(?i)hostname mismatch"):
        fetch_with_ssrf_guard(url, timeout=5, allow_private_origin=True)
