"""Operator-managed private-network exceptions for MCP OAuth discovery.

The allow-list is deliberately host-only and exact-match.  It relaxes only
the private-address portion of OAuth SSRF validation; HTTPS, userinfo,
same-origin, port, and dangerous-address checks remain in force.
"""

from __future__ import annotations

import ipaddress
import os
import re
import urllib.parse
from typing import Any

MCP_TRUSTED_PRIVATE_HOSTS_ENV = "TURNSTONE_MCP_OAUTH_TRUSTED_PRIVATE_HOSTS"
MCP_TRUSTED_PRIVATE_HOSTS_SETTING = "mcp.oauth_trusted_private_hosts"
MAX_TRUSTED_PRIVATE_HOSTS = 100

_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_trusted_private_host(raw: str) -> str:
    """Return a canonical exact hostname/IP or raise ``ValueError``."""
    value = str(raw).strip()
    if not value:
        raise ValueError("host must not be empty")
    if any(marker in value for marker in ("://", "/", "?", "#", "@", "*")):
        raise ValueError("use an exact hostname or IP address, without a URL or wildcard")

    # IP literals are accepted because urllib.parse exposes them as exact
    # hostname strings during discovery.  Brackets belong to URL syntax, not
    # to the configured host value.
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        pass

    if ":" in value:
        raise ValueError("ports are not allowed; enter only the exact hostname")
    value = value.rstrip(".").lower()
    try:
        ascii_value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("host is not a valid DNS name") from exc
    if len(ascii_value) > 253 or not ascii_value:
        raise ValueError("host is not a valid DNS name")
    if any(not _DNS_LABEL_RE.fullmatch(label) for label in ascii_value.split(".")):
        raise ValueError("host is not a valid DNS name")
    return ascii_value


def parse_trusted_private_hosts(raw: str | None) -> tuple[str, ...]:
    """Parse a comma/newline-separated host list, preserving first-seen order."""
    if raw is None or not str(raw).strip():
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,\n]", str(raw)):
        if not item.strip():
            continue
        host = normalize_trusted_private_host(item)
        if host not in seen:
            seen.add(host)
            result.append(host)
        if len(result) > MAX_TRUSTED_PRIVATE_HOSTS:
            raise ValueError(
                f"at most {MAX_TRUSTED_PRIVATE_HOSTS} trusted private hosts are allowed"
            )
    return tuple(result)


def merge_trusted_private_hosts(
    *, environment_value: str | None, manual_value: str | None
) -> list[dict[str, object]]:
    """Merge environment and user entries, with environment taking precedence."""
    environment_hosts = parse_trusted_private_hosts(environment_value)
    manual_hosts = parse_trusted_private_hosts(manual_value)
    merged: list[dict[str, object]] = [
        {"host": host, "source": "environment", "readonly": True} for host in environment_hosts
    ]
    environment_set = set(environment_hosts)
    merged.extend(
        {"host": host, "source": "manual", "readonly": False}
        for host in manual_hosts
        if host not in environment_set
    )
    if len(merged) > MAX_TRUSTED_PRIVATE_HOSTS:
        raise ValueError(
            f"at most {MAX_TRUSTED_PRIVATE_HOSTS} trusted private hosts are allowed in total"
        )
    return merged


def configured_trusted_private_hosts(config_store: Any) -> list[dict[str, object]]:
    """Read and merge the live environment and database-backed user setting."""
    manual = ""
    if config_store is not None:
        manual = str(config_store.get(MCP_TRUSTED_PRIVATE_HOSTS_SETTING, "") or "")
    return merge_trusted_private_hosts(
        environment_value=os.environ.get(MCP_TRUSTED_PRIVATE_HOSTS_ENV),
        manual_value=manual,
    )


def trusted_private_host_set(config_store: Any) -> frozenset[str]:
    """Return the effective exact-match set used by OAuth discovery."""
    return frozenset(str(entry["host"]) for entry in configured_trusted_private_hosts(config_store))


def url_uses_trusted_private_host(url: str, trusted_hosts: frozenset[str]) -> bool:
    """Whether *url* names one of the exact, canonical trusted hosts."""
    try:
        hostname = urllib.parse.urlparse(url).hostname
        return bool(hostname and normalize_trusted_private_host(hostname) in trusted_hosts)
    except ValueError:
        return False


__all__ = [
    "MAX_TRUSTED_PRIVATE_HOSTS",
    "MCP_TRUSTED_PRIVATE_HOSTS_ENV",
    "MCP_TRUSTED_PRIVATE_HOSTS_SETTING",
    "configured_trusted_private_hosts",
    "merge_trusted_private_hosts",
    "normalize_trusted_private_host",
    "parse_trusted_private_hosts",
    "trusted_private_host_set",
    "url_uses_trusted_private_host",
]
