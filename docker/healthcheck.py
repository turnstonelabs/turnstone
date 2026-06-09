#!/usr/bin/env python3
"""Health check for turnstone containers.

Usage: healthcheck.py <url>
Exit 0 if the endpoint returns {"status": "ok"} or {"status": "degraded"},
exit 1 otherwise. Uses only stdlib — no pip dependencies required.

When the node serves mTLS (tls.enabled), a plain-HTTP probe is rejected at
the socket, so on failure this script retries over HTTPS, presenting the
node's own certificate as the client cert and pinning the cluster CA. The
PEM files are the ones the server writes at boot under
$TURNSTONE_TLS_PEM_DIR (default: <tmpdir>/turnstone-tls). The host is
rewritten to "localhost" for the TLS attempt because the internal CA issues
DNS SANs only — certificate verification rejects a literal-IP dial.

When mTLS is disabled (the default), the plain probe succeeds and nothing
here changes: the PEM directory is never consulted.
"""

import json
import os
import ssl
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _check(url: str, context: ssl.SSLContext | None = None) -> None:
    """Probe one URL; raise if unreachable or the payload is unhealthy."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5, context=context) as resp:
        data = json.loads(resp.read().decode())
    if data.get("status") not in ("ok", "degraded"):
        raise RuntimeError(f"unhealthy payload: {data}")


def _pem_root() -> Path:
    """PEM runtime root.

    Must mirror turnstone.core.tls.tls_pem_runtime_dir — this script is
    standalone stdlib and cannot import turnstone; a drift-guard test in
    tests/test_docker_healthcheck.py pins the two together.
    """
    root_env = os.environ.get("TURNSTONE_TLS_PEM_DIR")
    return Path(root_env) if root_env else Path(tempfile.gettempdir()) / "turnstone-tls"


def _find_pem_dir() -> Path | None:
    """Locate the newest complete PEM dir written by the server at boot."""
    root = _pem_root()
    candidates = [
        d
        for d in root.glob("lacme-pem-*")
        if all((d / name).is_file() for name in ("fullchain.pem", "key.pem", "ca.pem"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def _tls_url(url: str) -> str:
    """Rewrite scheme to https and host to localhost, keeping port and path."""
    parts = urlsplit(url)
    netloc = f"localhost:{parts.port}" if parts.port else "localhost"
    return urlunsplit(("https", netloc, parts.path, parts.query, parts.fragment))


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: healthcheck.py <url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    try:
        _check(url)
        sys.exit(0)
    except Exception as plain_exc:
        pem_dir = _find_pem_dir()
        if pem_dir is None:
            print(f"Health check failed: {plain_exc}", file=sys.stderr)
            sys.exit(1)
        try:
            context = ssl.create_default_context(cafile=str(pem_dir / "ca.pem"))
            context.load_cert_chain(str(pem_dir / "fullchain.pem"), str(pem_dir / "key.pem"))
            _check(_tls_url(url), context=context)
            sys.exit(0)
        except Exception as tls_exc:
            print(
                f"Health check failed: plain: {plain_exc}; mtls: {tls_exc}",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
