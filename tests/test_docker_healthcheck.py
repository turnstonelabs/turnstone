"""Tests for docker/healthcheck.py — the container health probe.

Drives the real script via subprocess against real local listeners (plain
HTTP and mTLS with lacme-minted certs, the same CA path production uses),
mirroring how Docker invokes it.
"""

from __future__ import annotations

import http.server
import json
import os
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

lacme = pytest.importorskip("lacme")

SCRIPT = Path(__file__).parent.parent / "docker" / "healthcheck.py"


def run_healthcheck(url: str, pem_root: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Point the script at the test's PEM root — or at an empty dir to model
    # a plain-HTTP node with no TLS material on disk.
    env["TURNSTONE_TLS_PEM_DIR"] = str(pem_root) if pem_root else "/nonexistent"
    return subprocess.run(
        [sys.executable, str(SCRIPT), url],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = {"status": "ok"}

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def serve():
    """Factory that starts an HTTP(S) server on an ephemeral port and returns
    that port.

    Every server it starts is shut down + its serve_forever thread joined at
    teardown, so the thread never outlives the test (which would otherwise bleed
    into a later test's captured output / leak the listener).
    """
    started: list[tuple[http.server.HTTPServer, threading.Thread]] = []

    def _factory(handler_cls, ssl_context: ssl.SSLContext | None = None) -> int:
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        if ssl_context is not None:
            httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        started.append((httpd, thread))
        return httpd.server_address[1]

    yield _factory

    for httpd, thread in started:
        httpd.shutdown()  # break the serve_forever loop
        httpd.server_close()  # release the listening socket
        thread.join(timeout=5)


@pytest.fixture
def mtls_setup(tmp_path):
    """Mint a CA + node cert exactly as the server does, write PEM files
    under a runtime root, and build an mTLS server context requiring
    client certs (mirrors uvicorn's ssl_cert_reqs=CERT_REQUIRED)."""
    from lacme import CertificateAuthority, MemoryStore
    from lacme.mtls import write_pem_files

    from turnstone.core.tls import build_cert_hostnames

    ca = CertificateAuthority(store=MemoryStore())
    ca.init()
    bundle = ca.issue(build_cert_hostnames("http://node-1:8080", bind_host="0.0.0.0"))

    pem_root = tmp_path / "turnstone-tls"
    pem_root.mkdir()
    paths = write_pem_files(bundle, ca_pem=ca.root_cert_pem, directory=pem_root)

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(paths.cert), str(paths.key))
    server_ctx.load_verify_locations(str(paths.ca))
    server_ctx.verify_mode = ssl.CERT_REQUIRED

    return pem_root, server_ctx


# ── Plain HTTP (mTLS disabled — the default deployment) ─────────────────────


def test_plain_http_ok(serve):
    """Default path: plain probe succeeds, PEM dir never consulted."""
    port = serve(_Handler)
    result = run_healthcheck(f"http://127.0.0.1:{port}/health")
    assert result.returncode == 0, result.stderr


def test_plain_http_degraded_is_healthy(serve):
    """'degraded' (backend down, server up) still counts as container-healthy."""

    class Degraded(_Handler):
        payload = {"status": "degraded"}

    port = serve(Degraded)
    result = run_healthcheck(f"http://127.0.0.1:{port}/health")
    assert result.returncode == 0, result.stderr


def test_plain_http_bad_status_fails(serve):
    class Bad(_Handler):
        payload = {"status": "error"}

    port = serve(Bad)
    result = run_healthcheck(f"http://127.0.0.1:{port}/health")
    assert result.returncode == 1
    assert "unhealthy payload" in result.stderr


def test_server_down_fails():
    """Nothing listening: fail, with or without PEM material around."""
    result = run_healthcheck("http://127.0.0.1:9/health")
    assert result.returncode == 1
    assert "Health check failed" in result.stderr


# ── mTLS (tls.enabled) ───────────────────────────────────────────────────────


def test_mtls_probe_with_pem_dir(mtls_setup, serve):
    """The regression case: mTLS node + plain-HTTP probe URL.

    The plain attempt is rejected at the socket; the script must fall back
    to HTTPS with the node cert as client cert and report healthy."""
    pem_root, server_ctx = mtls_setup
    port = serve(_Handler, ssl_context=server_ctx)
    result = run_healthcheck(f"http://127.0.0.1:{port}/health", pem_root=pem_root)
    assert result.returncode == 0, result.stderr


def test_mtls_probe_without_pems_fails(mtls_setup, serve):
    """mTLS node but no PEM material on disk: the probe must fail."""
    _, server_ctx = mtls_setup
    port = serve(_Handler, ssl_context=server_ctx)
    result = run_healthcheck(f"http://127.0.0.1:{port}/health", pem_root=None)
    assert result.returncode == 1
    assert "Health check failed" in result.stderr


def test_mtls_unhealthy_payload_fails(mtls_setup, serve):
    """A reachable mTLS server with a bad payload is still unhealthy."""
    pem_root, server_ctx = mtls_setup

    class Bad(_Handler):
        payload = {"status": "error"}

    port = serve(Bad, ssl_context=server_ctx)
    result = run_healthcheck(f"http://127.0.0.1:{port}/health", pem_root=pem_root)
    assert result.returncode == 1
    assert "unhealthy payload" in result.stderr


def test_mtls_incomplete_pem_dir_fails(mtls_setup, tmp_path, serve):
    """A PEM dir missing the key is skipped, not half-used."""
    _, server_ctx = mtls_setup
    incomplete = tmp_path / "incomplete-root"
    d = incomplete / "lacme-pem-x"
    d.mkdir(parents=True)
    (d / "fullchain.pem").write_text("not a cert")
    (d / "ca.pem").write_text("not a cert")

    port = serve(_Handler, ssl_context=server_ctx)
    result = run_healthcheck(f"http://127.0.0.1:{port}/health", pem_root=incomplete)
    assert result.returncode == 1


# ── Drift guards (script re-encodes contracts it cannot import) ──────────────


def _load_script_module():
    """Load healthcheck.py as a module — docker/ is not a package."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("healthcheck_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_pem_root_matches_server(monkeypatch):
    """Drift guard: the script's default PEM root equals the server's.

    The script cannot import turnstone (standalone stdlib), so the default
    path literal is re-encoded; a rename on either side must fail here, not
    silently break mTLS probing in production."""
    from turnstone.core.tls import tls_pem_runtime_dir

    monkeypatch.delenv("TURNSTONE_TLS_PEM_DIR", raising=False)
    assert _load_script_module()._pem_root() == tls_pem_runtime_dir()


def test_find_pem_dir_accepts_real_pem_layout(monkeypatch, mtls_setup):
    """Drift guard: lacme's on-disk layout is accepted by _find_pem_dir.

    Pins the lacme-pem-* dir prefix and the fullchain/key/ca filename
    triplet against real write_pem_files output."""
    pem_root, _ = mtls_setup
    monkeypatch.setenv("TURNSTONE_TLS_PEM_DIR", str(pem_root))
    found = _load_script_module()._find_pem_dir()
    assert found is not None
    assert found.parent == pem_root
