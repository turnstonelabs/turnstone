"""The SDK's TLS arguments against a loopback node that requires a client certificate."""

import contextlib
import http.server
import json
import shutil
import socket
import ssl
import subprocess
import threading

import httpx2
import pytest

from turnstone.sdk import REQUEST_FAILURES
from turnstone.sdk._base import _BaseClient

_HOST = "node.example"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def pki(tmp_path):
    """One private CA's PEM files: a server leaf for the node and a client leaf."""
    from lacme import CertificateAuthority, MemoryStore
    from lacme.mtls import write_pem_files

    ca = CertificateAuthority(store=MemoryStore())
    ca.init()

    def _issue(name):
        return write_pem_files(ca.issue([name]), ca_pem=ca.root_cert_pem, directory=tmp_path)

    return _issue(_HOST), _issue("client.example")


@contextlib.contextmanager
def _serving(monkeypatch, server_paths, version):
    """Serve HTTPS on loopback as the node, requiring a client certificate from the CA."""
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        # anyio passes the host encoded, the standard library as a string.
        target = "127.0.0.1" if host in (_HOST, _HOST.encode()) else host
        return real_getaddrinfo(target, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = version
    context.load_cert_chain(str(server_paths.cert), str(server_paths.key))
    context.load_verify_locations(cafile=str(server_paths.ca))
    context.verify_mode = ssl.CERT_REQUIRED
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://{_HOST}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def node(monkeypatch, pki):
    # Under TLS 1.2 the node refuses a client without a certificate during the handshake, so the
    # refusal is always a ConnectError.
    with _serving(monkeypatch, pki[0], ssl.TLSVersion.TLSv1_2) as url:
        yield url


@pytest.fixture
def node_on_tls13(monkeypatch, pki):
    # Under TLS 1.3 the client finishes its handshake before the node checks the certificate, so
    # the refusal reaches the first read instead: as the alert itself, a raw ssl.SSLError, or as a
    # ReadError when the node's reset arrives first.
    with _serving(monkeypatch, pki[0], ssl.TLSVersion.TLSv1_3) as url:
        yield url


@pytest.mark.anyio
async def test_the_client_certificate_is_presented(node, pki):
    # ca_cert and client_cert together: httpx 0.28 dropped the certificate (#1425), and httpx2
    # refuses a CA path combined with a cert pair.
    server_paths, client_paths = pki
    async with _BaseClient(
        base_url=node,
        ca_cert=str(server_paths.ca),
        client_cert=str(client_paths.cert),
        client_key=str(client_paths.key),
    ) as client:
        assert await client._request("GET", "/health") == {"ok": True}


@pytest.mark.anyio
async def test_the_node_refuses_a_client_without_a_certificate(node, pki):
    server_paths, _ = pki
    async with _BaseClient(base_url=node, ca_cert=str(server_paths.ca)) as client:
        with pytest.raises(httpx2.ConnectError):
            await client._request("GET", "/health")


@pytest.mark.anyio
async def test_a_refusal_after_the_tls13_handshake_is_a_request_failure(node_on_tls13, pki):
    server_paths, _ = pki
    async with _BaseClient(base_url=node_on_tls13, ca_cert=str(server_paths.ca)) as client:
        with pytest.raises(REQUEST_FAILURES):
            await client._request("GET", "/health")


@pytest.mark.anyio
async def test_a_client_certificate_without_ca_cert_uses_the_default_trust(node, pki, monkeypatch):
    # Without ca_cert the context is httpx2's default, which SSL_CERT_FILE replaces.
    server_paths, client_paths = pki
    monkeypatch.setenv("SSL_CERT_FILE", str(server_paths.ca))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    async with _BaseClient(
        base_url=node, client_cert=str(client_paths.cert), client_key=str(client_paths.key)
    ) as client:
        assert await client._request("GET", "/health") == {"ok": True}


@pytest.mark.anyio
async def test_a_hashed_ca_directory_verifies_the_node(node, pki, tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("hashing the directory needs the openssl command")
    server_paths, client_paths = pki
    # The name ``openssl rehash`` gives the file; LibreSSL's openssl has ``x509 -hash`` but no
    # ``rehash``.
    subject_hash = subprocess.run(
        ["openssl", "x509", "-hash", "-noout", "-in", str(server_paths.ca)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ca_dir = tmp_path / "cas"
    ca_dir.mkdir()
    shutil.copy(server_paths.ca, ca_dir / f"{subject_hash}.0")
    async with _BaseClient(
        base_url=node,
        ca_cert=str(ca_dir),
        client_cert=str(client_paths.cert),
        client_key=str(client_paths.key),
    ) as client:
        assert await client._request("GET", "/health") == {"ok": True}


@pytest.mark.anyio
async def test_a_ca_directory_that_was_never_hashed_fails_the_request(node, pki, tmp_path):
    server_paths, client_paths = pki
    ca_dir = tmp_path / "cas"
    ca_dir.mkdir()
    shutil.copy(server_paths.ca, ca_dir / "ca.pem")
    async with _BaseClient(
        base_url=node,
        ca_cert=str(ca_dir),
        client_cert=str(client_paths.cert),
        client_key=str(client_paths.key),
    ) as client:
        with pytest.raises(httpx2.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            await client._request("GET", "/health")


def test_a_ca_bundle_that_cannot_be_read_fails_construction(tmp_path):
    with pytest.raises(OSError):
        _BaseClient(ca_cert=str(tmp_path / "missing.pem"))
