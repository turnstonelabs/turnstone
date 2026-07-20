"""Wire-safety projections for MCP server status dicts.

Shared by the node's internal MCP endpoints (turnstone/server.py) and
the console's coordinator-facing MCP arms (#725) so the two hosts
present one status schema over the wire.  Pure functions over plain
dicts — anything stateful (outcome classification, HTTP mapping,
logging) stays with the callers.
"""

from __future__ import annotations

from typing import Any

_SERVER_STATUS_PUBLIC_KEYS: tuple[str, ...] = (
    "connected",
    "tools",
    "resources",
    "prompts",
    "error",
    "transport",
    "circuit_open",
    "consecutive_failures",
)

_READ_STATUS_PUBLIC_KEYS: tuple[str, ...] = tuple(
    k for k in _SERVER_STATUS_PUBLIC_KEYS if k != "error"
)


def strip_server_status(full: dict[str, Any]) -> dict[str, Any]:
    """Project a status dict to the approve-scope public-safe key set.

    The full status dict embeds ``command`` (stdio argv) and ``url``
    (remote MCP endpoint) which are admin-only context. Approve-scoped
    callers (refresh/reconnect) get the verbose ``error`` text so an
    operator triaging a failure sees the underlying exception.

    Read-scope callers must use :func:`strip_server_status_for_read`
    instead — error strings can carry stdio binary paths
    (``FileNotFoundError: ... '/usr/local/bin/...'``) or internal MCP
    URLs (``httpx.ConnectError: ... 'https://internal/...'``) and
    those are equivalent to leaking ``command``/``url``.
    """
    return {k: full[k] for k in _SERVER_STATUS_PUBLIC_KEYS if k in full}


def strip_server_status_for_read(full: dict[str, Any]) -> dict[str, Any]:
    """Project a status dict for read-scope callers.

    Drops the verbose ``error`` text and replaces it with a coarse
    ``has_error: bool`` so dashboards can light up a failure indicator
    without leaking the underlying exception detail.
    """
    out = {k: full[k] for k in _READ_STATUS_PUBLIC_KEYS if k in full}
    out["has_error"] = bool(full.get("error"))
    return out


def public_server_status(mcp_mgr: Any, name: str) -> dict[str, Any]:
    """Strip ``command``/``url`` from ``get_server_status`` before returning over the wire."""
    # aggregate=True: the operator refresh/reconnect endpoints are approve-scoped
    # cluster actions with no single requesting user, so oauth_user servers report
    # the any-user warm-pool view (matching the admin console) rather than the
    # per-user default — which, with user_id=None, would render a warm, in-use
    # server as disconnected/empty right after a successful refresh.
    return strip_server_status(mcp_mgr.get_server_status(name, aggregate=True))
