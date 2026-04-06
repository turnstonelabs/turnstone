"""Collect auto-populated node metadata using stdlib only."""

from __future__ import annotations

import logging
import os
import platform
import socket
from typing import Any

log = logging.getLogger(__name__)


def _is_loopback_or_link_local(addr: str) -> bool:
    """Return True for loopback and link-local addresses."""
    return addr.startswith("127.") or addr == "::1" or addr.startswith("fe80:")


def _collect_interfaces() -> dict[str, list[str]]:
    """Best-effort host IP collection using stdlib.

    Returns a mapping from hostname to non-loopback IP addresses.
    Without psutil/netifaces, per-interface resolution is not available
    from stdlib alone, so we report resolved host addresses honestly.
    """
    result: dict[str, list[str]] = {}
    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        ips = sorted({str(a[4][0]) for a in addrs if not _is_loopback_or_link_local(str(a[4][0]))})
        if ips:
            result[hostname] = ips
    except OSError:
        log.debug("node_info: interface collection failed", exc_info=True)
    return result


def collect_node_info() -> dict[str, Any]:
    """Collect auto-populated node metadata.

    Returns a dict of ``{key: value}`` where values are JSON-serializable.
    Each field is collected independently — one failure does not block others.
    """
    info: dict[str, Any] = {}

    for key, fn in (
        ("hostname", socket.gethostname),
        ("fqdn", socket.getfqdn),
        ("os", platform.system),
        ("os_release", platform.release),
        ("arch", platform.machine),
        ("python", platform.python_version),
        ("cpu_count", os.cpu_count),
    ):
        try:
            val = fn()
            if val is not None:
                info[key] = val
        except Exception:
            log.debug("node_info: failed to collect %s", key, exc_info=True)

    try:
        ifaces = _collect_interfaces()
        if ifaces:
            info["interfaces"] = ifaces
    except Exception:
        log.debug("node_info: failed to collect interfaces", exc_info=True)

    return info
