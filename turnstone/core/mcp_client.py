"""MCP (Model Context Protocol) client manager.

Connects to external MCP tool servers and exposes their tools, resources,
and prompts alongside turnstone's built-in capabilities.

Architecture: the MCP SDK is fully async, but turnstone's ChatSession is
synchronous.  We bridge the two by running a dedicated asyncio event loop
in a daemon thread.  ``call_tool_sync`` dispatches coroutines onto that loop
via ``asyncio.run_coroutine_threadsafe``.

Refresh: two mechanisms keep tool/resource/prompt lists up-to-date:
  1. Push notifications — servers declaring ``listChanged`` on the
     respective capability trigger immediate refresh.
  2. Manual — ``/mcp refresh [server]`` triggers ``refresh_sync()``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import gc
import json
import random
import threading
import time
import urllib.parse
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio
import httpx
import mcp.types as mcp_types
from mcp import ClientSession, McpError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared._httpx_utils import (
    MCP_DEFAULT_SSE_READ_TIMEOUT,
    MCP_DEFAULT_TIMEOUT,
    McpHttpClientFactory,
)

from turnstone.core.config import load_config
from turnstone.core.log import get_logger
from turnstone.core.mcp_http_parsers import (
    MAX_INSUFFICIENT_SCOPE_REPORTED,
    is_valid_scope_token,
    parse_www_authenticate_error,
    parse_www_authenticate_scope,
)
from turnstone.core.mcp_oauth import (
    TokenLookupResult,
    emit_oauth_failure_audit,
    get_obo_access_token_classified,
    get_user_access_token_classified,
    is_user_scoped_auth,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

log = get_logger("turnstone.mcp")


# ---------------------------------------------------------------------------
# Transport URL hygiene (per-user OAuth bearer transmission)
# ---------------------------------------------------------------------------


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _validate_oauth_user_url(url: str) -> None:
    """Reject MCP server URLs that would transmit a bearer token in plaintext.

    Pool dispatch attaches the per-user OAuth bearer to the
    ``Authorization`` header; ``http://`` URLs would leak it in transit.
    Only the exact loopback hostnames in ``_LOOPBACK_HOSTS`` are exempt;
    a ``*.localhost`` suffix bypass is intentionally NOT honored because
    RFC 6761 localhost-zone resolution is configuration-dependent
    (custom resolvers, hosts file, split-horizon DNS, Docker overlays
    can map ``foo.localhost`` to non-loopback IPs and silently break
    the bearer-confidentiality guarantee).
    """
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme == "https":
        return
    hostname = (parsed.hostname or "").lower()
    if scheme == "http" and hostname in _LOOPBACK_HOSTS:
        return
    raise ValueError(
        "MCP servers with auth_type='oauth_user' must use https:// (loopback http:// excepted)"
    )


# ---------------------------------------------------------------------------
# Pool dispatch auth introspection (response-hook carrier)
# ---------------------------------------------------------------------------
#
# The MCP SDK's ``streamable_http`` transport raises
# :class:`httpx.HTTPStatusError` inside ``_handle_post_request`` and the
# enclosing ``post_writer`` swallows it (``mcp/client/streamable_http.py``
# logger.exception path). The dispatcher then sees only
# ``McpError(CONNECTION_CLOSED)`` with no status / headers preserved.
# To recover the upstream 401/403 we plug into the SDK's documented
# extension point — ``streamablehttp_client(httpx_client_factory=...)``
# — and pass a factory that builds the ``httpx.AsyncClient`` with a
# response hook. The hook fires after headers arrive but BEFORE
# ``raise_for_status()`` runs, so the carrier is populated before the
# SDK swallow.
#
# Forward-compat: ``streamablehttp_client`` is ``@deprecated`` in SDK
# 1.27 in favour of ``streamable_http_client(http_client=...)`` which
# accepts a pre-built client. The same factory pattern translates
# 1:1 against the new entry point when we migrate.


# Defensive cap on the number of tools we accept from any single MCP
# server's ``tools/list`` response. Real servers expose at most a few
# dozen tools; a misconfigured or hostile upstream returning thousands
# would amplify both memory (one OpenAI tool dict per entry) and
# downstream BM25 reindex cost. Mirrors the ``_MAX_ERROR_LEN`` /
# ``MAX_INSUFFICIENT_SCOPE_REPORTED`` defensive ceilings: we truncate
# rather than reject so partial visibility beats zero visibility, and
# emit a warning so operators can investigate.
_MAX_TOOLS_PER_SERVER = 1000

# Defensive caps mirroring ``_MAX_TOOLS_PER_SERVER`` for the resource and
# prompt list paths discovered in :meth:`MCPClientManager._connect_one_pool`
# (RFC §3.2 for resources, §3.3 for prompts). Real MCP servers expose at
# most a few dozen of each; a
# hostile or misconfigured upstream returning thousands would amplify
# memory (one dict per entry) plus downstream rendering cost. Truncate
# rather than reject so partial visibility beats zero visibility, and
# emit a warning so operators can investigate.
_MAX_RESOURCES_PER_SERVER = 1000
_MAX_RESOURCE_TEMPLATES_PER_SERVER = 1000
_MAX_PROMPTS_PER_SERVER = 1000

# Upper bound on concurrent per-user pool primes at ChatSession start. Servers
# are warmed in parallel (so one slow/unreachable upstream can't stall the rest)
# but capped so a deployment with many oauth_user servers doesn't fire a
# thundering herd of connects on every session start.
_PRIME_MAX_CONCURRENCY = 4

# One row per ``*/list_changed`` kind: notification type → kind label.
# The static AND pool notification handlers drive all three catalog kinds
# through this table (plus their kind → bound-method maps, which stay
# mypy-checked attribute access rather than name-string tables) so the
# debounce/coalesce/log/spawn protocol exists exactly once per path — a
# protocol change cannot silently diverge between kinds. Exact-type lookup
# is safe: the SDK's discriminated-union parser instantiates these concrete
# classes, never subclasses.
_LIST_CHANGED_KINDS: dict[type, str] = {
    mcp_types.ToolListChangedNotification: "tools",
    mcp_types.ResourceListChangedNotification: "resources",
    mcp_types.PromptListChangedNotification: "prompts",
}


def try_prime_user_pools(
    mcp_client: Any,
    user_id: str | None,
    *,
    require_live_listener: bool = False,
    context: str = "prime",
) -> None:
    """Best-effort per-user pool prime: schedule and swallow, never raise.

    The ONE copy of the fire-and-forget prime idiom shared by the
    session-construction, acting-user-change, and OIDC capture-success
    call sites — three hand-synced copies had already drifted.
    ``mcp_client`` is duck-typed (session and auth tests stub it): a
    client without the prime surface, or a falsy ``user_id``, is a
    silent no-op, matching the guards this replaces. Failures never
    propagate — a prime is an optimisation, and neither login nor
    session construction may fail on it. ``require_live_listener``
    gates on an open session to heal (the OIDC capture site: priming on
    every routine SSO re-login would warm transports and mint tokens
    for users with nothing open, at deployment scale).
    """
    if not user_id or not hasattr(mcp_client, "prime_user_pools"):
        return
    try:
        if require_live_listener and not (
            hasattr(mcp_client, "has_live_session_listener")
            and mcp_client.has_live_session_listener(user_id)
        ):
            return
        mcp_client.prime_user_pools(user_id)
    except Exception:
        log.debug(
            "MCP pool prime scheduling failed (%s) user=%s",
            context,
            user_id,
            exc_info=True,
        )


# How long a node trusts its own pending-consent DELETE before re-running it on
# the next dispatch success. Bounds two things at once: the hot-path SQL rate
# (at most one DELETE per (user, server) per window) and the staleness of a
# consent badge written by ANOTHER node after this node's last clear.
_PENDING_CONSENT_CLEAR_TTL_SECONDS = 300.0

# Size threshold that triggers opportunistic pruning of the cleared-pairs TTL
# map. Purely memory hygiene — a pruned pair costs one extra SQL DELETE on its
# next success, never a correctness change.
_PENDING_CONSENT_CLEARED_MAX = 4096


# ``mcp.client.streamable_http._send_session_terminated_error`` synthesizes this
# (non-standard, POSITIVE) JSON-RPC code *client-side* when a held
# ``mcp-session-id`` 404s after the MCP server restarted and dropped its session
# map. It is NOT a documented MCP constant, and the SDK deliberately discards the
# server's own 404 body ("Session not found", code ``-32600``) — so it is pinned
# here, greppable for the next SDK bump. No spec-compliant server emits a POSITIVE
# 32600, which is what makes the code a safe, deterministic dead-transport signal
# to key off. The application-controlled message is NOT matched: a healthy
# session-owning server can legitimately return a protocol error whose message is
# "Session terminated".
_SDK_SESSION_TERMINATED_CODE = 32600


def _is_dead_transport(exc: BaseException) -> bool:
    """True when *exc* means the MCP session's transport is dead and the
    session must be torn down and rebuilt (vs a protocol-level rejection
    from a still-healthy connection).

    The streamable-http SDK holds the session over anyio in-memory streams.
    Three distinct death modes all mean "reconnect me", not "the server
    rejected my request":

    1. **Local stream torn down** — the GET/SSE or POST stream died (idle close,
       peer reset, keep-alive expiry) and the ``ClientSession`` object survives
       with a closed write stream, so ``list_tools``/``call_tool`` raises
       :class:`anyio.ClosedResourceError` / :class:`anyio.BrokenResourceError`.
    2. **Transport-swallowed** — the SDK's ``post_writer`` swallows the upstream
       error and the caller sees ``McpError(CONNECTION_CLOSED)`` (-32000); or the
       underlying httpx connection is gone / unrecoverable mid-exchange. Every
       ``httpx.NetworkError`` ({Connect,Read,Write,Close}Error), the Connect/Read/
       Write timeouts, and ``httpx.RemoteProtocolError`` qualify — notably a
       read/idle timeout on a long-lived stream, which is NOT a builtin
       ``TimeoutError`` and would otherwise be misread as a healthy "other"
       failure. ``httpx.PoolTimeout`` is deliberately EXCLUDED: it means
       connection-pool saturation, not a dead connection — evicting the session
       wouldn't relieve the pressure and would trip the shared breaker for all
       users on transient load.
    3. **Server-side session lost** — the MCP server RESTARTED and dropped its
       session map, so our held ``mcp-session-id`` is unknown. The server returns
       HTTP 404 and the SDK synthesizes ``McpError(code=32600, "Session
       terminated")`` (see ``streamable_http._send_session_terminated_error``),
       discarding the server's own 404 body. Keyed off that synthesized code
       ALONE (a positive 32600 no compliant server emits); the message is
       application-controlled, so a healthy session-owning server that returns a
       protocol error reading "Session terminated" stays breaker-safe.

    The raw-socket variants (``BrokenPipeError`` / ``ConnectionResetError`` /
    ``EOFError``) are kept for the stdio transport and defense-in-depth.
    """
    if isinstance(
        exc,
        anyio.ClosedResourceError
        | anyio.BrokenResourceError
        | BrokenPipeError
        | ConnectionResetError
        | EOFError
        # NetworkError == {Connect,Read,Write,Close}Error (connection gone). The
        # Connect/Read/Write timeouts mean a dead/hung connection; PoolTimeout is
        # EXCLUDED (pool saturation, not a dead session — eviction can't relieve
        # it and would trip the shared breaker under load). RemoteProtocolError
        # (peer broke framing) is dead; LocalProtocolError (our bug) stays out.
        | httpx.NetworkError
        | httpx.ConnectTimeout
        | httpx.ReadTimeout
        | httpx.WriteTimeout
        | httpx.RemoteProtocolError,
    ):
        return True
    if isinstance(exc, McpError):
        err = exc.error
        code = getattr(err, "code", None)
        if code == mcp_types.CONNECTION_CLOSED:
            return True
        # Server-restarted-session loss: match the SDK's deterministic synthesized
        # code ALONE. The message is application-controlled — a healthy
        # session-owning server (e.g. a game/shell MCP server) can legitimately
        # reject a stale id with a protocol error whose message is
        # "Session terminated" / "session not found", and matching on it would
        # tear down that live session and trip the shared per-server breaker for
        # every user. The client never sees those messages for a REAL dead
        # transport (the SDK discards the server's 404 body and synthesizes code
        # 32600), so keying off the code loses no coverage.
        if code == _SDK_SESSION_TERMINATED_CODE:
            return True
    return False


@dataclass
class _AuthCapture:
    """Carrier populated by the response hook on 4xx upstream responses."""

    status: int | None = None
    www_authenticate: str | None = None


class _PoolDispatchRetryRequested(BaseException):  # noqa: N818
    """Module-private signal: the auth_401 branch wants the sync caller to retry.

    Inherits :class:`BaseException` (not ``Exception``) so that nothing in
    the SDK / anyio path accidentally swallows it inside an ``except
    Exception`` block. ``_dispatch_pool_sync`` is the only caller that
    catches it and the only handler that re-issues the dispatch on a
    fresh ``asyncio.Task`` via ``run_coroutine_threadsafe``.
    """


class _CarrierAuthSignal(Exception):  # noqa: N818
    """Internal signal raised when the response hook captures a 4xx mid-call.

    Raised from ``_dispatch_pool_with_entry`` when the carrier's fired
    event wins the race against ``session.call_tool``. The dispatcher's
    ``_classify_failure(exc, capture=...)`` resolves the actual auth
    class (``auth_401`` / ``auth_403``) from the carrier's ``status``,
    so this exception is just a structural placeholder — it never
    surfaces to callers.
    """


def _make_capturing_http_factory(
    capture: _AuthCapture, fired_event: asyncio.Event | None = None
) -> McpHttpClientFactory:
    """Return an ``httpx`` factory that records 4xx auth signals into ``capture``.

    The hook is ``async`` because :class:`httpx.AsyncClient` invokes
    response hooks via ``await hook(response)`` — a sync function would
    return ``None`` and ``await None`` raises ``TypeError`` inside the
    SDK's :meth:`client.stream` call. Even though our work is purely
    synchronous (read ``status_code`` and a header), the contract
    requires an awaitable. The first attempt's hook would still
    populate the carrier (the body runs before the ``await``), but the
    ``TypeError`` poisons the SDK's anyio TaskGroup teardown so the
    next ``streamablehttp_client(...)`` invocation surfaces a stray
    ``CancelledError`` from inside its own scope. Empirically verified
    via a minimal repro: a sync hook breaks back-to-back connects in
    the same process; the async form does not.
    """

    async def _hook(response: httpx.Response) -> None:
        # Only record on auth-relevant statuses to keep the carrier
        # focused. ``capture`` is mutated in place; the dispatcher
        # consults it after ``call_tool`` returns/raises. No I/O, no
        # other awaits — the hook stays cancellation-safe.
        #
        # Use ``get_list(...)[0]`` rather than ``get(...)`` so a
        # malicious upstream that emits multiple ``WWW-Authenticate``
        # headers cannot inject auth-params into the parser via the
        # comma-joined value httpx returns from ``get(...)``. Repeated
        # headers join with ``, `` which the RFC 7235 tokenizer would
        # otherwise consume as a continuation of the first challenge,
        # silently folding attacker scopes into the parsed dict. We
        # discard every challenge after the first; defence-in-depth
        # mirror lives in ``parse_www_authenticate_bearer``.
        status = response.status_code
        if status in (401, 403):
            capture.status = status
            headers = response.headers.get_list("www-authenticate")
            capture.www_authenticate = headers[0] if headers else None
            if fired_event is not None:
                # Wakes the dispatcher's race in ``_dispatch_pool_with_entry``.
                # See the docstring on _CarrierAuthSignal for why call_tool
                # cannot be relied on to propagate the failure on a reused
                # session.
                fired_event.set()

    def _factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "event_hooks": {"response": [_hook]},
        }
        if timeout is None:
            kwargs["timeout"] = httpx.Timeout(
                MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT
            )
        else:
            kwargs["timeout"] = timeout
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return _factory


# ---------------------------------------------------------------------------
# MCP ↔ OpenAI schema conversion
# ---------------------------------------------------------------------------


def _mcp_to_openai(server_name: str, tool: Any) -> dict[str, Any]:
    """Convert a single MCP tool definition to OpenAI function-calling format.

    The tool name is prefixed ``mcp__{server}__{original}`` to avoid
    collisions with built-in tools and to identify the owning server.
    """
    input_schema = getattr(tool, "inputSchema", None) or {
        "type": "object",
        "properties": {},
    }
    description = getattr(tool, "description", "") or ""
    return {
        "type": "function",
        "function": {
            "name": f"mcp__{server_name}__{tool.name}",
            "description": description,
            "parameters": input_schema,
        },
    }


def _cap_server_tools(server_name: str, tools: list[Any]) -> list[Any]:
    """Apply ``_MAX_TOOLS_PER_SERVER`` cap with operator-visible warning.

    Identity for inputs at or below the cap (no copy); slice + warn on
    overflow. Caller is responsible for converting the returned list to
    the OpenAI shape via :func:`_mcp_to_openai`.
    """
    if len(tools) <= _MAX_TOOLS_PER_SERVER:
        return tools
    log.warning(
        "MCP server '%s' returned %d tools — truncating to %d "
        "(_MAX_TOOLS_PER_SERVER cap). Misconfigured or hostile upstream?",
        server_name,
        len(tools),
        _MAX_TOOLS_PER_SERVER,
    )
    return tools[:_MAX_TOOLS_PER_SERVER]


def _cap_server_resources(server_name: str, resources: list[Any]) -> list[Any]:
    """Apply ``_MAX_RESOURCES_PER_SERVER`` cap with operator-visible warning.

    Identity for inputs at or below the cap (no copy); slice + warn on
    overflow. Mirrors :func:`_cap_server_tools` for the resource list path.
    """
    if len(resources) <= _MAX_RESOURCES_PER_SERVER:
        return resources
    log.warning(
        "MCP server '%s' returned %d resources — truncating to %d "
        "(_MAX_RESOURCES_PER_SERVER cap). Misconfigured or hostile upstream?",
        server_name,
        len(resources),
        _MAX_RESOURCES_PER_SERVER,
    )
    return resources[:_MAX_RESOURCES_PER_SERVER]


def _cap_server_resource_templates(server_name: str, templates: list[Any]) -> list[Any]:
    """Apply ``_MAX_RESOURCE_TEMPLATES_PER_SERVER`` cap with operator-visible warning.

    Identity for inputs at or below the cap (no copy); slice + warn on
    overflow. Mirrors :func:`_cap_server_resources` for the resource
    template list path (RFC §3.2 templates are a separate catalog from
    concrete resources but share the per-server amplification risk).
    """
    if len(templates) <= _MAX_RESOURCE_TEMPLATES_PER_SERVER:
        return templates
    log.warning(
        "MCP server '%s' returned %d resource templates — truncating to %d "
        "(_MAX_RESOURCE_TEMPLATES_PER_SERVER cap). Misconfigured or hostile upstream?",
        server_name,
        len(templates),
        _MAX_RESOURCE_TEMPLATES_PER_SERVER,
    )
    return templates[:_MAX_RESOURCE_TEMPLATES_PER_SERVER]


def _cap_server_prompts(server_name: str, prompts: list[Any]) -> list[Any]:
    """Apply ``_MAX_PROMPTS_PER_SERVER`` cap with operator-visible warning.

    Identity for inputs at or below the cap (no copy); slice + warn on
    overflow. Mirrors :func:`_cap_server_tools` for the prompt list path.
    """
    if len(prompts) <= _MAX_PROMPTS_PER_SERVER:
        return prompts
    log.warning(
        "MCP server '%s' returned %d prompts — truncating to %d "
        "(_MAX_PROMPTS_PER_SERVER cap). Misconfigured or hostile upstream?",
        server_name,
        len(prompts),
        _MAX_PROMPTS_PER_SERVER,
    )
    return prompts[:_MAX_PROMPTS_PER_SERVER]


# ---------------------------------------------------------------------------
# Per-server state containers
# ---------------------------------------------------------------------------


@dataclass
class StaticServerState:
    """Per-server state for auth_type ∈ {none, static}. Name-keyed only.

    The upcoming per-user pool integration introduces PoolEntryState as
    the (user, server)-keyed sibling for auth_type=oauth_user. Together
    with the typed map declarations (dict[str, StaticServerState] vs
    dict[tuple[str, str], PoolEntryState]), this makes accidental
    cross-keying lookups easier to catch in review and rejected by mypy.
    """

    name: str
    session: Any | None = None
    # Transport ownership. The owner task is the ONE task that enters and
    # exits the transport + ClientSession context managers (anyio cancel
    # scopes are host-task-bound: a scope whose host task has finished can
    # never be exited, and anyio then re-delivers cancellation to it in a
    # ``call_soon`` loop forever — the SDK #2147 100%-CPU spin). The exit
    # stack is deliberately a LOCAL of the owner coroutine, unreachable from
    # other tasks, so nothing can ``aclose()`` it cross-task. Teardown asks
    # the owner to close via ``close_requested`` (see
    # ``_teardown_static_session`` for the close protocol).
    owner_task: asyncio.Task[None] | None = None
    close_requested: asyncio.Event | None = None
    streams: tuple[Any, Any] | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    # Dispatch counter for the health-loop eviction interlock (mirrors
    # ``PoolEntryState.in_flight``): the liveness ping skips and never evicts a
    # server with ``in_flight > 0`` so a long-running ``call_tool`` pins its
    # session against teardown. Incremented/decremented on the mcp-loop around
    # the session op by ``_static_session_op``.
    in_flight: int = 0
    supports_list_changed: bool = False
    supports_resources: bool = False
    supports_prompts: bool = False
    supports_resource_list_changed: bool = False
    supports_prompt_list_changed: bool = False


@dataclass
class PoolEntryState:
    """Per-(user, server) state for auth_type = oauth_user.

    open_lock has no default — the mcp-loop invariant forbids allocating
    an asyncio.Lock outside the mcp-loop; the pool integration allocates
    lazily inside connect coroutines.

    ``in_flight`` is a dispatch counter used by the eviction interlock:
    eviction skips entries with ``in_flight > 0`` so a long-running
    ``call_tool`` can release ``open_lock`` while still pinning the
    entry's session against teardown.

    ``auth_capture`` is bound once at first connect (passed to the
    httpx response hook factory); the hook closes over this object for
    the life of the underlying ``httpx.AsyncClient``. Each dispatch
    resets the carrier's fields under ``open_lock`` before
    ``call_tool`` and reads them after — a per-dispatch carrier would
    fail silently on session reuse because the hook is bound at
    connect time, not per dispatch.
    """

    key: tuple[str, str]  # (user_id, server_name)
    open_lock: asyncio.Lock
    session: Any | None = None
    # Transport ownership. The owner task is the ONE task that enters and
    # exits the transport + ClientSession context managers (anyio cancel
    # scopes are host-task-bound: a scope whose host task has finished can
    # never be exited, and anyio then re-delivers cancellation to it in a
    # ``call_soon`` loop forever — the SDK #2147 100%-CPU spin). The exit
    # stack is deliberately a LOCAL of the owner coroutine, unreachable from
    # other tasks, so nothing can ``aclose()`` it cross-task. Teardown asks
    # the owner to close via ``close_requested`` (see ``_teardown_pool_entry``
    # for the close protocol).
    owner_task: asyncio.Task[None] | None = None
    close_requested: asyncio.Event | None = None
    streams: tuple[Any, Any] | None = None
    # Catalog state — populated lazily once per-user discovery wires
    # in; left ``None`` here so 200-entry pools don't retain 600 empty
    # list objects.
    tools: list[dict[str, Any]] | None = None
    resources: list[dict[str, Any]] | None = None
    prompts: list[dict[str, Any]] | None = None
    last_used: float = 0.0
    in_flight: int = 0
    # Access token this session's httpx client was connected with. The bearer is
    # frozen into the client's STATIC headers at connect (_connect_one_pool), so
    # when the stored token later refreshes we compare against this to detect a
    # stale session and reconnect (rebind the current token) proactively —
    # instead of replaying the stale bearer and eating a guaranteed upstream 401.
    bound_token: str | None = None
    auth_capture: _AuthCapture = field(default_factory=_AuthCapture)
    # Set by the response hook when the carrier captures a 4xx; awaited
    # by ``_dispatch_pool_with_entry``'s race against ``call_tool``.
    # Must be allocated on the mcp-loop (per :class:`asyncio.Event`'s
    # loop-binding contract); ``_ensure_pool_entry`` runs on the loop
    # so the dataclass default_factory is safe.
    auth_fired_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Capability flags mirror ``StaticServerState`` so the pool path can
    # gate resource / prompt discovery the same way (RFC §3.2). Tools
    # are always discovered, so no ``supports_tools`` flag is needed —
    # but ``listChanged`` for tools is also implicit (the notification
    # handler is always registered). Resources and prompts get explicit
    # presence flags because we skip discovery entirely when the
    # capability is absent.
    supports_resources: bool = False
    supports_prompts: bool = False
    supports_resource_list_changed: bool = False
    supports_prompt_list_changed: bool = False

    def drop_session(self) -> None:
        """Drop the cached session AND its paired plaintext bearer copy.

        The ONE way to null ``session``: ``bound_token`` is read only
        under a live session (stale-rebind detection) and overwritten
        at reconnect, so it is dead the moment the session goes — and
        an entry may cool indefinitely after any drop, which must not
        retain a plaintext credential for the life of the user's
        sessions. A site that nulled ``session`` directly would
        silently re-open that hole.
        """
        self.session = None
        self.bound_token = None


# ---------------------------------------------------------------------------
# Client manager
# ---------------------------------------------------------------------------


class MCPClientManager:
    """Manages connections to one or more MCP servers.

    Runs a background asyncio event loop in a daemon thread and exposes
    synchronous methods for tool discovery and invocation.
    """

    def __init__(
        self,
        server_configs: dict[str, dict[str, Any]],
    ) -> None:
        self._server_configs = server_configs
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._exit_stack: AsyncExitStack | None = None

        # Per-server state for auth_type ∈ {none, static}.  Each entry holds
        # session/owner-task/streams/catalog/capability flags for one
        # name-keyed connection.  The upcoming per-user pool integration introduces a
        # sibling pool-entry map for auth_type=oauth_user; static entries
        # always live here.
        self._static_servers: dict[str, StaticServerState] = {}

        self._tools: list[dict[str, Any]] = []
        # prefixed_name -> (server_name, original_tool_name).
        # ``_db_servers_to_config`` strips ``oauth_user`` rows on the way
        # into ``_static_servers``, so any tool that landed in ``_tool_map``
        # is by construction static-path; ``_resolve_pool_target`` uses
        # presence-in-map (not the auth_type column) to short-circuit.
        self._tool_map: dict[str, tuple[str, str]] = {}
        self._connected = threading.Event()
        self._error: str | None = None
        # Names managed by the DB (added via reconcile_sync / add_server_sync).
        # Config-file servers loaded at startup are NOT in this set and
        # will never be removed by reconcile_sync.
        self._db_managed: set[str] = set()
        # Per-server last-error tracking (set on failure, cleared on success)
        self._last_error: dict[str, str] = {}
        self._MAX_ERROR_LEN = 256

        # Listener infrastructure (tool-change callbacks for ChatSession).
        # Each entry is ``(user_id, callback)``: ``user_id=None`` is the
        # admin / global listener (fires on every tool-change), a string
        # ``user_id`` fires only on changes scoped to that user OR on
        # global static-path changes. RFC §3.3.
        self._listeners: list[tuple[str | None, Callable[[], None]]] = []
        self._listeners_lock = threading.Lock()

        # Merged resource catalog
        self._resources: list[dict[str, Any]] = []
        self._resource_map: dict[str, tuple[str, str]] = {}  # uri → (server, uri)
        # Phase 7b: each entry is ``(user_id, callback)`` mirroring the
        # tool-listener shape (RFC §3.3) — ``user_id=None`` is the admin
        # / global listener (fires on every change), a string ``user_id``
        # fires only on changes scoped to that user OR on global static
        # changes.
        self._resource_listeners: list[tuple[str | None, Callable[[], None]]] = []
        self._resource_listeners_lock = threading.Lock()

        # Merged prompt catalog
        self._prompts: list[dict[str, Any]] = []
        self._prompt_map: dict[str, tuple[str, str]] = {}  # prefixed → (server, original)
        # Phase 7b: see ``_resource_listeners`` for the tuple shape rationale.
        self._prompt_listeners: list[tuple[str | None, Callable[[], None]]] = []
        self._prompt_listeners_lock = threading.Lock()

        # Template prefix → (server_name, full_template_uri) for URI expansion
        self._template_prefixes: dict[str, tuple[str, str]] = {}

        # Governance storage (optional — set via set_storage())
        self._storage: Any = None
        self._sync_lock = threading.Lock()

        # Circuit breaker (per-server) — prevents repeated calls to broken servers
        self._consecutive_failures: dict[str, int] = {}
        self._circuit_open_until: dict[str, float] = {}  # monotonic timestamp
        self._circuit_trip_count: dict[str, int] = {}  # backoff exponent

        # Notification debounce, keyed ``(server, kind)`` — kind-scoped to
        # match the kind-scoped refreshes: a server-scoped stamp would drop
        # a different-kind notification inside the window outright, with no
        # parked runner to observe the change.
        self._last_notification_refresh: dict[tuple[str, str], float] = {}
        # Coalescing markers for spawned static ``list_changed`` refreshes,
        # keyed ``(server, kind)``: set at spawn, cleared the moment the
        # runner acquires the per-name connect lock (before its list call).
        # While set, further notifications for the server+kind are dropped —
        # the parked runner's fresh list will observe their change — bounding
        # the connect lock's waiter queue at ONE parked runner per
        # server+kind. Mirrors ``_pool_refresh_pending``.
        self._static_refresh_pending: set[tuple[str, str]] = set()

        # Last refresh outcome (Phase 9 — admin status indicator).  Per-
        # server tuple of ``(unix_ts, outcome)`` where outcome is one of
        # ``ok`` or ``error:<ExceptionClassName>``.  Populated by
        # ``_refresh_server`` on every call (success and failure paths),
        # which means manual operator-driven refresh (``refresh_sync``)
        # AND the ``_cb_auto_reconnect`` follow-up that schedules
        # ``_refresh_server`` directly both populate the field — adding
        # a future schedule site only needs to call ``_refresh_server``
        # to participate.  Read by the admin status endpoint to render
        # the per-server "last refresh" pill.  No initial entry is
        # created at server-register time — absence surfaces as ``null``
        # in the admin JSON, which the UI renders as "never".
        self._last_refresh: dict[str, tuple[float, str]] = {}

        # Per-(user, server) state for auth_type=oauth_user. Loop-bound:
        # mutated only on the mcp-loop. Sync threads interact via
        # ``asyncio.run_coroutine_threadsafe``.
        self._user_pool_entries: dict[tuple[str, str], PoolEntryState] = {}
        # (user, server) keys with a session-start prime in flight — collapses
        # concurrent primes (multiple ChatSession starts) before the redundant
        # token/server-row DB reads. Mutated only on the single mcp-loop thread,
        # so no lock is needed.
        self._priming_keys: set[tuple[str, str]] = set()
        # LRU tracking: monotonic last-access timestamp per pool key.
        self._user_pool_last_used: dict[tuple[str, str], float] = {}
        # Per-key lock guarding open / dispatch / close. Allocated lazily
        # inside ``_ensure_pool_entry`` (which runs only on the mcp-loop)
        # so each ``asyncio.Lock`` binds to the correct loop on first use.
        self._user_pool_locks: dict[tuple[str, str], asyncio.Lock] = {}

        # Per-(user, server) catalog state. Tools live on
        # ``PoolEntryState.tools`` (the dataclass already carries the
        # field — it's populated lazily by per-user discovery on first
        # connect). ``_user_tool_map`` is the per-user prefixed-name
        # index, mirroring static ``_tool_map``: outer key is ``user_id``,
        # inner is ``prefixed_name → (server_name, original_name)``.
        # Phase 7b lights up the resource/prompt sibling dicts below.
        self._user_tool_map: dict[str, dict[str, tuple[str, str]]] = {}
        # Per-user merged tool list (one snapshot per user_id), updated
        # atomically alongside ``_user_tool_map`` in
        # ``_rebuild_user_tool_map``. ``get_tools(user_id=...)`` reads
        # this via a single dict-get (atomic under GIL) so sync-thread
        # callers (ChatSession) never iterate ``_user_pool_entries``
        # concurrently with the mcp-loop's mutations of the same dict
        # (insert in ``_ensure_pool_entry`` / the SOLE pop in
        # ``_close_pool_entry_if_idle``'s full-drop path).
        self._user_tools: dict[str, list[dict[str, Any]]] = {}

        # Per-user resource catalog. Mirrors ``_user_tool_map`` /
        # ``_user_tools``: outer key is ``user_id``, inner is
        # ``uri → (server_name, uri)``. Loop-only writes via
        # :meth:`_rebuild_user_resource_map`; sync-thread reads via a
        # single dict-get on ``_user_resources`` (atomic under GIL).
        # RFC §3.2 — Phase 7b lights this up.
        self._user_resource_map: dict[str, dict[str, tuple[str, str]]] = {}
        self._user_resources: dict[str, list[dict[str, Any]]] = {}
        # Per-user template-prefix index for resource URI expansion.
        # ``_user_template_prefixes[user_id][prefix] = (server, full_template_uri)``.
        # Used by :meth:`_match_template`'s per-user-first lookup branch
        # (scope decision 0.1).
        self._user_template_prefixes: dict[str, dict[str, tuple[str, str]]] = {}

        # Per-user prompt catalog. Mirrors ``_user_tool_map`` /
        # ``_user_tools`` for prompts: outer key is ``user_id``, inner
        # is ``prefixed_name → (server_name, original_name)``.
        self._user_prompt_map: dict[str, dict[str, tuple[str, str]]] = {}
        self._user_prompts: dict[str, list[dict[str, Any]]] = {}

        # Notification debounce for pool sessions, keyed
        # ``((user_id, server), kind)``. Mirrors ``_last_notification_refresh``
        # (static) but per-pool-key so a noisy server in one user's pool
        # doesn't suppress a refresh in another user's pool of the same
        # server — and kind-scoped for the same reason as the static stamp:
        # refreshes are kind-scoped, so a key-scoped stamp would drop a
        # different-kind notification inside the window outright.
        self._last_pool_notification_refresh: dict[tuple[tuple[str, str], str], float] = {}
        # Coalescing markers for spawned ``list_changed`` refreshes, keyed
        # ``((user_id, server), kind)``: set at spawn, cleared the moment
        # the runner acquires ``open_lock`` (before its list call). While
        # set, further notifications for the key+kind are dropped — the
        # parked runner's fresh list will observe their change — bounding
        # the lock's waiter queue at ONE parked runner per key+kind.
        self._pool_refresh_pending: set[tuple[tuple[str, str], str]] = set()

        # Pool tuning (read at construction; falls back to defaults).
        mcp_cfg = load_config("mcp")
        self._user_pool_idle_ttl_s = float(mcp_cfg.get("user_session_idle_ttl_seconds", 600))
        self._user_pool_lru_max = int(mcp_cfg.get("user_session_lru_max", 200))
        # Background token-freshness sweep cadence (seconds). Keeps every
        # consented oauth_user grant hot for unattended / autonomous work. Kept
        # comfortably under the shortest common access-token lifetime so a token
        # is refreshed before it lapses; 240 s clears the usual 300 s floor with
        # margin. A value <= 0 DISABLES the sweep (the task is never started); a
        # positive value is floored at ``_MIN_USER_TOKEN_SWEEP_S`` so a
        # fat-fingered tiny cadence can't turn the loop into a CPU/DB/AS
        # busy-loop (``asyncio.sleep(0)`` yields without delay).
        raw_sweep_s = float(mcp_cfg.get("user_token_sweep_seconds", 240))
        self._user_token_sweep_s = (
            max(self._MIN_USER_TOKEN_SWEEP_S, raw_sweep_s) if raw_sweep_s > 0 else 0.0
        )
        # How long a consented grant's refresh token may sit un-exercised before
        # the sweep FORCE-refreshes it (even while the access token is still
        # fresh) to keep it alive for a provider that ages out idle refresh
        # tokens — the exact unattended-work failure the sweep exists to prevent.
        # <= 0 disables keepalive (only near-expiry refreshes). Must sit safely
        # under the shortest provider refresh-token idle timeout; 1800s (30m)
        # clears a ~1h idle window with margin. Effective floor is the sweep
        # cadence (a grant can't be force-refreshed more than once per tick).
        self._user_token_refresh_keepalive_s = float(
            mcp_cfg.get("user_token_refresh_keepalive_seconds", 1800)
        )
        # Static-server health loop: liveness-ping cadence (seconds). Each tick
        # pings every connected static server (evicting a dead-but-idle one so it
        # is reconnected) and retries any disconnected one on a capped, jittered,
        # forever backoff. The SDK gives up reconnecting after 2 attempts with no
        # backoff (verified: mcp 1.28.1) and never for other transports, so this
        # is Turnstone's own reconnect — nothing upstream to lean on. <= 0
        # disables the loop entirely.
        self._static_health_check_s = float(mcp_cfg.get("static_health_check_seconds", 30))
        # Rate-limit clock for the orphaned-scope disarm backstop
        # (:meth:`_maybe_disarm_orphaned_scopes`). Monotonic; 0.0 = never ran.
        self._last_scope_disarm: float = 0.0

        # OAuth integration. ``set_app_state`` wires app_state in after the
        # lifespan has built the token store / OAuth helpers; pool dispatch
        # asserts non-None when it actually runs, so static-only callers
        # never hit it.
        self._app_state: Any = None

        # In-memory cache of server names whose ``auth_type='oauth_user'``.
        # ``_db_servers_to_config`` strips oauth_user rows on the way into
        # ``_server_configs``, so neither that dict nor ``_static_servers``
        # carries auth_type. This set is populated alongside ``reconcile_sync``
        # / ``set_oauth_user_servers`` so per-turn callers (web_search
        # backend resolution) can answer "is this server pool-backed?"
        # without a SQL roundtrip.
        self._oauth_user_server_names: set[str] = set()
        # Sibling registry for auth_type='oauth_obo' servers (issue #551):
        # also pool-backed (per-user sessions), but with NO per-server
        # consent flow — tokens mint from the user's single captured
        # credential, so the priming / keep-alive-sweep / consent sites that
        # iterate ``_oauth_user_server_names`` deliberately exclude these.
        self._obo_server_names: set[str] = set()
        # (user, server) → monotonic time of this node's last DB-confirmed
        # pending-consent DELETE. The dispatch-success clear skips its SQL
        # DELETE while the entry is younger than the TTL (keeps the hot path
        # write-free) and re-runs it once the entry ages out — so a
        # pending-consent row written by ANOTHER node after this node's last
        # clear still self-heals within one TTL window of active dispatching.
        # (A pure cleared-once set suppressed the clear forever: node A's
        # entry never invalidated when node B wrote a fresh row.) A failure
        # observed on THIS node re-arms the pair immediately
        # (_write_pending_consent pops it). Entries are pruned
        # opportunistically on insert — memory hygiene, not correctness.
        self._pending_consent_cleared: dict[tuple[str, str], float] = {}

        # Idle-eviction task handle. Scheduled lazily on the mcp-loop the
        # first time a pool entry is created (start() runs before pool
        # rows exist, so deferring keeps the task count at zero in
        # static-only deployments).
        self._user_pool_eviction_task: asyncio.Task[None] | None = None

        # Token-freshness sweep task handle. Like the eviction task it is a
        # dedicated handle (not in ``_background_tasks``); UNLIKE it, the sweep
        # is started once in ``_connect_all`` because a consented grant needs
        # keeping-hot even before any pool entry exists. Its tick early-returns
        # when no ``oauth_user`` server is configured, so a static-only / no-auth
        # deployment pays only a per-tick set-emptiness check — no AS calls, no
        # token-table scans.
        self._user_token_sweep_task: asyncio.Task[None] | None = None
        # ``(user, server)`` pairs whose dead-grant / undecryptable state the
        # sweep has already surfaced, so it badges + logs ONCE on the transition
        # rather than every tick. Pruned to the currently-consented set each
        # pass, so a recovered or re-consented grant re-arms the surfacing.
        # Loop-only mutation.
        self._token_sweep_warned: set[tuple[str, str]] = set()
        # Static-server health loop handle (started once in ``_connect_all``).
        self._static_health_task: asyncio.Task[None] | None = None
        # Per-server connect serialization. ``_connect_one`` tears down and
        # rebuilds the SHARED ``StaticServerState``; two concurrent reconnects
        # for one server (health loop + a dispatch's ``_cb_auto_reconnect``, or
        # an operator refresh) would interleave that teardown and corrupt the
        # entry. This per-name lock serializes them. Lazily created on the
        # mcp-loop (the asyncio.Lock allocation invariant).
        self._static_connect_locks: dict[str, asyncio.Lock] = {}
        # Per-server reconnect backoff state (health loop only, loop-bound):
        # consecutive-failure ``attempt`` count and the monotonic time of the
        # next allowed reconnect attempt. Capped + jittered, no attempt limit.
        self._static_reconnect_attempt: dict[str, int] = {}
        self._static_reconnect_next: dict[str, float] = {}
        # Per-server next liveness-ping deadline (monotonic).
        self._static_next_ping: dict[str, float] = {}

        # Strong references to fire-and-forget background tasks (catalog
        # refreshes etc.).  ``create_task`` alone keeps only a weak ref — an
        # untracked task can be GC'd mid-flight, and its exception surfaces
        # as "Task exception was never retrieved" at GC time instead of
        # being logged where it happened.  See ``_spawn_background``.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _ensure_static_state(self, name: str) -> StaticServerState:
        """Get or create the StaticServerState for ``name``.

        Returns an empty state on first access; subsequent fields are populated
        as connect proceeds.
        """
        state = self._static_servers.get(name)
        if state is None:
            state = StaticServerState(name=name)
            self._static_servers[name] = state
        return state

    async def _ensure_pool_entry(self, key: tuple[str, str]) -> PoolEntryState:
        """Get or create the PoolEntryState for ``(user_id, server_name)``.

        MUST run on the mcp-loop — the lazily-allocated ``asyncio.Lock``
        binds to whatever loop is current at construction, and the pool
        dispatch path relies on every per-key lock being bound to
        ``self._loop``.
        """
        entry = self._user_pool_entries.get(key)
        if entry is None:
            lock = self._user_pool_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._user_pool_locks[key] = lock
            entry = PoolEntryState(key=key, open_lock=lock)
            self._user_pool_entries[key] = entry
            # First pool entry — start the idle-eviction loop. Deferred
            # until the first pool key materializes so static-only
            # deployments never spawn the task.
            if self._user_pool_eviction_task is None:
                self._user_pool_eviction_task = asyncio.create_task(self._user_pool_eviction_loop())
        return entry

    def set_app_state(self, app_state: Any) -> None:
        """Wire the OAuth ``app.state`` into the manager.

        Called from the lifespan after ``initialize_mcp_crypto_state`` and
        ``initialize_mcp_oauth_state`` have populated the token store,
        OAuth HTTP client, metadata cache, and refresh-lock map. Required
        before any ``auth_type='oauth_user'`` dispatch; static-only
        deployments may leave it unset.
        """
        self._app_state = app_state

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch background event loop and connect to all configured servers."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp-loop")
        self._thread.start()

        future = asyncio.run_coroutine_threadsafe(self._connect_all(), self._loop)
        self._connected.wait(timeout=30)
        # Surface any exception from _connect_all (unlikely — per-server errors are caught)
        if future.done() and not future.cancelled():
            exc = future.exception()
            if exc:
                self._error = str(exc)
                log.error("MCP initialization error: %s", self._error)

    async def _connect_all(self) -> None:
        """Connect to every configured server (runs on the background loop)."""
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()

        for name, cfg in self._server_configs.items():
            try:
                await self._connect_one(name, cfg)
            except asyncio.CancelledError:
                raise  # propagate so the background task can be cleanly stopped
            except (Exception, BaseExceptionGroup) as exc:
                # ``BaseExceptionGroup`` explicitly: anyio task groups wrap a
                # transport failure that includes a stray CancelledError (an
                # accept-then-RST server, a cancel-scope collapse) into a
                # BaseException-derived group that ``except Exception`` MISSES.
                # Before this arm, one such server at startup killed
                # ``_connect_all`` before the health/sweep loops below were
                # ever created — silently disabling all autonomous recovery.
                log.warning("Failed to connect MCP server '%s'", name, exc_info=True)
                self._set_error(name, f"{type(exc).__name__}: {exc}")
                self._cb_record_failure(name)

        self._connected.set()

        # Start the background token-freshness sweep (once, on the mcp-loop).
        # Keeps every consented oauth_user grant hot for unattended work and
        # surfaces dead grants proactively. Started here rather than lazily (like
        # the eviction task) because a grant needs keeping-hot even in a
        # deployment that has not yet created a pool entry; the tick self-gates
        # to a no-op when no oauth_user server is configured. Skipped entirely
        # when disabled via config (cadence <= 0).
        if self._user_token_sweep_s > 0 and self._user_token_sweep_task is None:
            self._user_token_sweep_task = asyncio.create_task(self._user_token_sweep_loop())

        # Start the static-server health loop (once, on the mcp-loop). Self-heals
        # static connections that the SDK's bounded reconnect / other transports
        # abandon — the autonomous trigger Turnstone otherwise lacks (all other
        # reconnect paths are dispatch- or operator-driven). Skipped when disabled
        # via config (cadence <= 0).
        if self._static_health_check_s > 0 and self._static_health_task is None:
            self._static_health_task = asyncio.create_task(self._static_health_loop())

    _CONNECT_TIMEOUT = 30  # seconds — prevents hung connections on broken remotes
    _TCP_PROBE_TIMEOUT = 5  # seconds — fast TCP pre-flight for HTTP transports

    # Circuit breaker constants
    _CB_FAILURE_THRESHOLD = 3
    _CB_BASE_COOLDOWN = 30.0  # seconds
    _CB_MAX_COOLDOWN = 300.0  # 5 minutes

    # Static-server reconnect backoff (health loop). Capped exponential + full
    # jitter, retry FOREVER (no attempt limit): a server that returns after a
    # long outage reconnects within ~a minute, and a permanently-misconfigured
    # one costs at most one attempt per ``_STATIC_RECONNECT_MAX_S``. The cap is
    # deliberately tighter than ``_CB_MAX_COOLDOWN`` (which gates dispatch
    # fail-fast, a different clock) so recovery is prompt.
    _STATIC_RECONNECT_BASE_S = 1.0
    _STATIC_RECONNECT_MAX_S = 60.0
    # Liveness-ping timeout. Deliberately generous (a slow-but-working server
    # must not churn): a ping that times out is treated as "slow, not dead" —
    # rescheduled, NOT evicted (only a ``_is_dead_transport`` failure evicts).
    # Well beyond the 120s dispatch call is unnecessary; a ping is a lightweight
    # round-trip, but heavy servers / congested links can still take seconds.
    _STATIC_HEALTH_PING_TIMEOUT_S = 30.0
    # Outer bound on a single autonomous reconnect ATTEMPT. The transport
    # owner bounds only the connect phases (``_CONNECT_TIMEOUT`` each); the
    # post-handshake ``list_tools``/``list_resources``/``list_prompts`` run in
    # the calling task and are unbounded, so a server that handshakes then
    # stalls discovery would wedge the loop AND hold the per-name lock
    # forever. Bound from the caller side with headroom for a worst-case
    # handshake plus fast discovery — safe to cancel now: the transport cms
    # live in the owner task, which is closed via the one-cancel protocol.
    _STATIC_RECONNECT_ATTEMPT_TIMEOUT_S = float(_CONNECT_TIMEOUT + 15)

    # Caller-side wait for a reconnect routed through ``_ensure_static_connected``
    # (a dispatch's ``_cb_auto_reconnect``, operator ``reconnect_sync`` /
    # ``remove_server_sync``). MUST exceed ``_STATIC_RECONNECT_ATTEMPT_TIMEOUT_S``
    # so the primitive's inner bound always fires FIRST — surfacing a clean
    # ``TimeoutError`` the primitive converts, cleans up, and records on the
    # breaker — instead of the caller cancelling mid-attempt and leaving a
    # half-discovered session installed with no breaker record, or an operator
    # teardown silently timing out behind a slow reconnect that holds the lock.
    _STATIC_RECONNECT_CALLER_TIMEOUT_S = _STATIC_RECONNECT_ATTEMPT_TIMEOUT_S + 10.0

    # Transport-owner close protocol (see ``_teardown_static_session``).
    # Grace for the GRACEFUL phase: the parked owner is asked to close via its
    # event and unwinds the transport cms in-task; the SDK's streamable-http
    # ``__aexit__`` may issue a session-termination DELETE, so allow it a few
    # seconds before escalating.
    _OWNER_CLOSE_GRACE_S = 5.0
    # Grace for the ESCALATED phase: after the single ``cancel()``. If the
    # unwind is STILL running when this expires, the owner is left to finish
    # on its own — it is self-contained and completing solo is harmless; a
    # SECOND cancel is what must never happen (it abandons anyio scope exits
    # mid-flight, minting the exact zombie this design removes).
    _OWNER_CANCEL_GRACE_S = 5.0
    # Rate limit for the orphaned-scope disarm sweep (``gc.get_objects`` walk;
    # cheap enough on failure paths, too heavy to run on every suppressed
    # close in a storm).
    _SCOPE_DISARM_MIN_INTERVAL_S = 30.0

    # Notification debounce
    _NOTIFICATION_DEBOUNCE = 5.0  # seconds between refreshes per server

    # Pool eviction loop tick interval (per-iteration sleep, seconds).
    _POOL_EVICTION_TICK_S = 30.0

    # Floor for the config-driven token-freshness sweep cadence. A positive
    # ``user_token_sweep_seconds`` below this is clamped up so a misconfigured
    # tiny value can't busy-loop the sweep; <= 0 disables it (handled at start).
    _MIN_USER_TOKEN_SWEEP_S = 30.0

    # Short grace before the FIRST sweep after connect, so a restart surfaces a
    # grant that died during downtime quickly (rather than after a full cadence)
    # without hammering a cold cluster the instant every node boots.
    _USER_TOKEN_SWEEP_STARTUP_GRACE_S = 5.0

    # Best-effort lock acquire timeout for the eviction pass; eviction must
    # never block on a contested per-key lock so the next tick retries.
    _POOL_EVICTION_LOCK_ACQUIRE_TIMEOUT_S = 0.05

    # -- circuit breaker (per-server) -----------------------------------------

    @staticmethod
    def _capped_exponential(base: float, cap: float, attempt: int) -> float:
        """``min(cap, base * 2**attempt)`` with the exponent clamped.

        The ONE capped-exponential formula — shared by the circuit-breaker
        cooldown (:meth:`_cb_record_failure`) and the health loop's reconnect
        ceiling (:meth:`_static_reconnect_delay`) — so cap/exponent policy
        can't drift between the two. ``attempt`` is unbounded on the health
        path (retry forever); the clamp keeps ``2**n`` from exploding into a
        bignum before ``min`` discards it. Jitter policy stays with the
        callers (full jitter on the health path, name-seeded 10% on the
        breaker).
        """
        ceiling: float = min(cap, base * (2 ** min(attempt, 32)))
        return ceiling

    def _cb_check(self, name: str) -> tuple[bool, bool]:
        """Check circuit breaker state for *name*.

        Returns ``(is_open, cooldown_expired)``.  When the circuit is closed
        both values are False.  When open, *cooldown_expired* indicates
        whether a probe attempt is allowed.
        """
        deadline = self._circuit_open_until.get(name)
        if deadline is None:
            return False, False
        now = time.monotonic()
        if now >= deadline:
            return True, True  # half-open: allow one probe
        return True, False  # still in cooldown

    def _cb_record_failure(self, name: str) -> None:
        """Record a failure against *name*, potentially opening the circuit."""
        count = self._consecutive_failures.get(name, 0) + 1
        self._consecutive_failures[name] = count
        # Guard: don't extend an already-open deadline.  Additional failures
        # while open still accumulate in _consecutive_failures, so the circuit
        # re-opens immediately after the next half-open probe fails (count is
        # already >= threshold).
        if count >= self._CB_FAILURE_THRESHOLD and name not in self._circuit_open_until:
            trips = self._circuit_trip_count.get(name, 0)
            cooldown = self._capped_exponential(
                self._CB_BASE_COOLDOWN, self._CB_MAX_COOLDOWN, trips
            )
            # Per-server jitter seeded from server name (varies across process
            # restarts via PYTHONHASHSEED, which is desirable — each cluster
            # node gets different jitter to avoid thundering herd).
            jitter = random.Random(hash(name)).random() * cooldown * 0.1
            self._circuit_open_until[name] = time.monotonic() + cooldown + jitter
            self._circuit_trip_count[name] = trips + 1
            log.warning(
                "MCP circuit open for '%s': %d consecutive failures, cooldown %.0fs",
                name,
                count,
                cooldown + jitter,
            )

    def _cb_record_success(self, name: str) -> None:
        """Record a successful operation for *name*, decaying circuit state.

        Decays trip count by 1 rather than resetting to 0, so a chronically
        flapping server escalates its backoff over time instead of always
        restarting at the minimum cooldown.
        """
        self._consecutive_failures.pop(name, None)
        self._circuit_open_until.pop(name, None)
        trips = self._circuit_trip_count.get(name, 0)
        if trips > 1:
            self._circuit_trip_count[name] = trips - 1
        else:
            self._circuit_trip_count.pop(name, None)

    def _cb_clear(self, name: str) -> None:
        """Remove all circuit breaker state for *name*."""
        self._consecutive_failures.pop(name, None)
        self._circuit_open_until.pop(name, None)
        self._circuit_trip_count.pop(name, None)

    # -- safe transport helpers ------------------------------------------------

    async def _pre_close_streams(self, key: str | tuple[str, str]) -> None:
        """Close MCP transport streams before the owner unwinds its transport.

        Pre-closing unblocks anyio transport tasks stuck on zero-buffer
        ``send()`` calls, preventing the CPU busy-loop from SDK #2147.

        Accepts either a static server name (``str``) or a pool key
        (``(user_id, server_name)`` tuple). The two paths share this
        helper but address different state maps.
        """
        if isinstance(key, tuple):
            entry = self._user_pool_entries.get(key)
            if entry is None or entry.streams is None:
                return
            streams = entry.streams
            entry.streams = None  # take-and-clear pattern
            for s in streams:
                with contextlib.suppress(Exception):
                    await s.aclose()
            return
        state = self._static_servers.get(key)
        if state is None or state.streams is None:
            return
        streams = state.streams
        state.streams = None  # take-and-clear pattern
        for s in streams:
            with contextlib.suppress(Exception):
                await s.aclose()

    async def _tcp_probe(self, key: str | tuple[str, str], url: str) -> None:
        """Fast TCP connect check before entering the MCP transport context.

        Fails fast when the server is unreachable, avoiding the anyio
        cancel-scope orphan bug that causes 100% CPU spin.

        Accepts either a static server name or a pool key; only the error
        message differs (the probe itself is keyless).
        """
        from urllib.parse import urlparse

        label = f"{key[1]}@{key[0]}" if isinstance(key, tuple) else key
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            raise ConnectionError(f"MCP server '{label}' has invalid URL (no hostname): {url}")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            raise ConnectionError(f"MCP server '{label}' has invalid port in URL: {url}") from None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self._TCP_PROBE_TIMEOUT,
            )
            writer.close()
            await writer.wait_closed()
        except (TimeoutError, OSError) as exc:
            raise ConnectionError(
                f"MCP server '{label}' unreachable at {host}:{port}: {exc}"
            ) from None

    def _maybe_disarm_orphaned_scopes(self, context: str) -> int:
        """Disarm anyio cancel scopes that can never drain (rate-limited backstop).

        anyio's ``CancelScope._deliver_cancellation`` reschedules itself via
        ``call_soon`` for as long as ANY task remains registered in the scope —
        including tasks that are already ``done()``, on which ``task.cancel()``
        is a silent no-op. A cancelled scope whose registered tasks have all
        finished (host task abandoned mid-exit, or a transport task group whose
        child died after the connecting task returned) therefore spins the
        event loop at 100% CPU forever (verified against anyio 4.14.1; no
        upstream fix as of that release). The owner-task architecture prevents
        Turnstone's static and pool paths from ever creating that state; this
        sweep is the belt-and-suspenders for anything else (SDK internals, a
        not-yet-migrated path, future regressions).

        Only scopes where EVERY registered task is done are touched — by then
        no task can ever exit the scope, so cancelling the armed handle and
        clearing the task set cannot suppress a real cancellation delivery.
        (Coverage note: the unit tests pin the all-done gate and the field
        manipulation with a synthetic handle; a REAL self-rescheduling
        ``_deliver_cancellation`` storm is prevented structurally by the owner
        architecture, so no test drives one through this sweep.)

        MUST run on the mcp-loop, and only touches scopes HOSTED on it: the
        ``gc.get_objects()`` walk is process-global, and in ``turnstone-server``
        this process also runs FastAPI/httpx anyio scopes on the MAIN loop —
        cancelling another loop's ``call_soon`` handle or clearing a set that
        loop may be iterating is a cross-thread reach with no thread-safety
        contract. Foreign-loop orphans are that loop's problem to fix. The
        guard below ENFORCES the mcp-loop requirement rather than trusting
        callers (a suppressed close can fire before ``start()`` or after
        ``shutdown()``, when there is no mcp-loop to be on), and a skipped
        call does not advance the rate-limit clock — a legitimate on-loop
        sweep may be needed immediately after.
        Returns the number of scopes disarmed. Rate-limited because the walk
        is O(heap).
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return 0
        if self._loop is None or running is not self._loop:
            return 0
        now = time.monotonic()
        if now - self._last_scope_disarm < self._SCOPE_DISARM_MIN_INTERVAL_S:
            return 0
        self._last_scope_disarm = now
        try:
            from anyio._backends._asyncio import CancelScope as _AnyioCancelScope
        except ImportError:  # pragma: no cover - anyio is a hard dependency
            return 0
        disarmed = 0
        for obj in gc.get_objects():
            if not isinstance(obj, _AnyioCancelScope):
                continue
            handle = getattr(obj, "_cancel_handle", None)
            tasks = getattr(obj, "_tasks", None)
            if handle is None or tasks is None:
                continue
            try:
                host = getattr(obj, "_host_task", None)
                if host is None or host.get_loop() is not self._loop:
                    continue
                if any(not t.done() for t in tasks):
                    continue
                handle.cancel()
                obj._cancel_handle = None
                tasks.clear()
                disarmed += 1
            except Exception:  # never let the backstop become its own failure
                log.debug("Failed to disarm a cancel scope", exc_info=True)
        if disarmed:
            log.warning(
                "MCP: disarmed %d orphaned anyio cancel scope(s) after %s "
                "(each was re-delivering cancellation every loop iteration)",
                disarmed,
                context,
            )
        return disarmed

    def _drop_static_session_and_stamp(self, name: str, state: StaticServerState) -> None:
        """Null the session AND pop its notification debounce stamp (paired).

        The static twin of :meth:`_drop_session_and_stamp` (pool). The
        stamp must not outlive the transport: the keep-stamp-on-failure
        design leans on every teardown popping it, so a reconnected
        transport's first ``list_changed`` refreshes immediately instead
        of being debounced against a pre-collapse stamp. An eviction site
        that nulled the session directly would silently re-open that
        stale-stamp hole. Safe when the session is already gone: both
        halves are idempotent.
        """
        state.session = None
        for kind in _LIST_CHANGED_KINDS.values():
            self._last_notification_refresh.pop((name, kind), None)

    async def _teardown_static_session(self, name: str) -> None:
        """Tear down a static server's session/transport (the ONE canonical order).

        Shared by :meth:`_connect_one_locked`'s stale-guard,
        :meth:`remove_server_sync`, and shutdown so a future ordering fix lands
        in one place. MUST run under the per-name connect lock (shutdown is
        exempt: the health loop and dispatch drivers are already stopped).
        Callers decide WHEN teardown is safe — live-session reuse and the
        ``in_flight`` interlock are enforced by :meth:`_ensure_static_connected`,
        not here. No-op when the server has no state.

        Close protocol (the anyio host-task invariant): the transport +
        ``ClientSession`` cms were entered by the server's OWNER task and can
        only be exited by it — an exit stack ``aclose()`` from any other task
        raises anyio's cross-task scope-exit error, and a scope whose host
        task has finished can never drain, which arms anyio's
        ``_deliver_cancellation`` retry into a permanent ``call_soon`` storm
        (the SDK #2147 100%-CPU spin this design removes). So:

        1. null the session (concurrent dispatch reads see "disconnected");
        2. pre-close the transport streams (unblocks anyio transport tasks
           stuck on zero-buffer ``send()`` and lets the SDK's readers/writers
           exit promptly);
        3. ask the owner to close (``close_requested.set()``) and give its
           in-task unwind ``_OWNER_CLOSE_GRACE_S``;
        4. escalate to ONE ``cancel()`` and wait ``_OWNER_CANCEL_GRACE_S``;
        5. if it is STILL unwinding, leave it to finish solo (it is
           self-contained; a SECOND cancel would abandon a scope exit
           mid-flight and mint the exact zombie this protocol exists to
           prevent) and run the orphaned-scope disarm sweep as a backstop.
        """
        state = self._static_servers.get(name)
        if state is None:
            return
        self._drop_static_session_and_stamp(name, state)
        owner = state.owner_task
        close_requested = state.close_requested
        state.owner_task = None
        state.close_requested = None
        # Signal BEFORE the first await: if this coroutine is itself cancelled
        # mid-teardown, the owner must already have its marching orders — a
        # parked owner that never learns of the close would hold the transport
        # open forever.
        if close_requested is not None:
            close_requested.set()
        await self._pre_close_streams(name)
        if owner is None or owner.done():
            return
        _done, pending = await asyncio.wait({owner}, timeout=self._OWNER_CLOSE_GRACE_S)
        if pending:
            owner.cancel()
            _done, pending = await asyncio.wait({owner}, timeout=self._OWNER_CANCEL_GRACE_S)
        if pending:
            log.warning(
                "MCP transport owner for '%s' still unwinding after close+cancel; "
                "leaving it to finish on its own",
                name,
            )
            self._maybe_disarm_orphaned_scopes(f"slow owner unwind for '{name}'")

    def _static_connect_lock_for(self, name: str) -> asyncio.Lock:
        """Return the per-server connect lock for ``name`` (lazily created).

        MUST be called on the mcp-loop — the asyncio.Lock allocation invariant.
        """
        lock = self._static_connect_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._static_connect_locks[name] = lock
        return lock

    async def _connect_one(self, name: str, cfg: dict[str, Any]) -> None:
        """Connect to a single MCP server, serialized per server.

        Wraps :meth:`_connect_one_locked` in the per-name connect lock so a
        health-loop reconnect, a dispatch ``_cb_auto_reconnect``, and an operator
        refresh can never interleave teardown/rebuild on the shared
        ``StaticServerState`` (which would corrupt the entry or leak a stack).
        No caller holds the lock before calling in, and the body never re-enters
        ``_connect_one`` for the same name, so there is no reentrancy risk.
        """
        if "__" in name:
            log.error("MCP server name '%s' contains '__' (reserved delimiter), skipping", name)
            return
        async with self._static_connect_lock_for(name):
            await self._connect_one_locked(name, cfg)

    async def _ensure_static_connected(
        self, name: str, cfg: dict[str, Any], *, defer_if_busy: bool = True
    ) -> Any:
        """Idempotently (re)connect static server *name* — the ONE lazy path.

        Every lazy/autonomous reconnect driver — the health loop
        (:meth:`_static_reconnect_one`), a dispatch's :meth:`_cb_auto_reconnect`,
        and :meth:`_refresh_all`'s reconnect branch — routes through here so the
        per-name lock / session-reuse / ``in_flight`` / config-recheck / breaker
        decisions live in exactly one place. The operator's
        :meth:`reconnect_sync` deliberately does NOT: that is a FORCE rebuild
        which tears down even a live session by design.

        MUST run on the mcp-loop. All decisions are made UNDER the per-name
        connect lock (concurrent callers QUEUE, then land in the reuse branch):

        1. **Config re-check** — a concurrent :meth:`remove_server_sync` pops
           the config (and retires the lock object) before tearing down;
           rebuilding from the caller's pre-lock ``cfg`` snapshot would
           resurrect the removed server. The freshest config wins — *cfg* is
           the caller's eligibility proof, superseded here.
        2. **Reuse-if-live** — another driver already (re)connected while we
           queued; a live session is NEVER torn down and rebuilt here.
        3. **``in_flight`` guard** — ``session is None`` with ``in_flight > 0``
           means a sibling call is still running on the old (evicted-but-open)
           stack; :meth:`_connect_one_locked`'s stale-guard teardown would
           abort it mid-flight. Defer — the next driver pass reconnects once
           it drains (mirrors the pool path's ``_close_pool_entry_if_idle``).
        4. **Bounded connect** — :meth:`_connect_one_locked` under
           ``_STATIC_RECONNECT_ATTEMPT_TIMEOUT_S`` from the caller side (the
           owner task's internal bounds cover only the connect phases, not
           discovery). Cancelling the caller here is SAFE: the transport cms
           live in the owner task, which is closed via the one-cancel
           protocol and unwinds in-task — a caller-side cancel can no longer
           abandon an anyio scope mid-exit (the old 100%-CPU zombie).

        The circuit breaker is OWNED here for connect outcomes — callers must
        not record the connect again. Success clears only the OPEN-CIRCUIT
        DEADLINE (dispatch flows again) and deliberately NOT
        ``_consecutive_failures``: a connect-ok / calls-fail server must still
        escalate to a trip; a real dispatch success is what resets the count
        (:meth:`_cb_record_success`). Failure records one breaker failure and
        re-raises.

        Returns the session on success or reuse; ``None`` on a deliberate skip
        (server removed, or busy with an in-flight call); raises on a real
        connect failure. A genuine task cancellation propagates untouched — no
        breaker record, since a cancelled attempt proves nothing about the
        server.
        """
        if "__" in name:
            # Mirrors _connect_one's reserved-delimiter guard: such a name can
            # never connect; treat as a deliberate skip.
            log.error("MCP server name '%s' contains '__' (reserved delimiter), skipping", name)
            return None
        lock = self._static_connect_lock_for(name)
        async with lock:
            # (1) Config re-check. The lock-identity check closes the
            # remove -> re-add race: remove_server_sync retires the lock object
            # after teardown, so a waiter still holding the OLD lock must not
            # connect concurrently with a NEW-lock holder after a re-add.
            fresh_cfg = self._server_configs.get(name)
            if fresh_cfg is None or self._static_connect_locks.get(name) is not lock:
                return None
            cfg = fresh_cfg
            state = self._static_servers.get(name)
            # (2) Reuse-if-live.
            if state is not None and state.session is not None:
                return state.session
            # (3) in_flight guard — AUTONOMOUS callers only (``defer_if_busy``).
            # A sibling call is still draining on the old (evicted-but-open)
            # stack; a rebuild's stale-guard teardown would abort it. The health
            # loop and ``_refresh_all`` defer (they retry on their own schedule);
            # a DISPATCH is a deliberate user action that NEEDS the session now,
            # so it does not defer — it reconnects (the pre-unification behavior),
            # accepting it may tear a sibling down rather than hard-fail a
            # reachable server.
            if defer_if_busy and state is not None and state.in_flight > 0:
                return None
            # (4) Bounded connect; the breaker is owned here. The inner bound is
            # STRICTLY below every caller's wait (``_STATIC_RECONNECT_CALLER_
            # TIMEOUT_S``), so it fires first and asyncio.timeout converts the
            # cancel to TimeoutError inside the lock — the caller never cancels a
            # live attempt out from under us.
            try:
                async with asyncio.timeout(self._STATIC_RECONNECT_ATTEMPT_TIMEOUT_S):
                    await self._connect_one_locked(name, cfg)
            except BaseException as exc:
                # ANY non-success exit — an ``except Exception`` connect error,
                # the converted inner TimeoutError, or a bare CancelledError (a
                # caller's sync boundary giving up early, or a genuine shutdown)
                # — must not leave a half-discovered session installed. We hold
                # the lock, so no concurrent driver installed a fresh session.
                #
                # A CancelledError proves nothing about the server (the caller
                # may have given up before the connect completed) — skip the
                # breaker record so a spurious cancel doesn't inflate the
                # failure count for a healthy server. The caller provides its
                # own accounting (or doesn't — shutdown state is discarded).
                if not isinstance(exc, asyncio.CancelledError):
                    self._cb_record_failure(name)
                await self._teardown_static_session(name)
                raise
            state = self._static_servers.get(name)
            if state is None or state.session is None:
                # e.g. a stdio config with no command "connects" without a
                # session — a real failure for a reconnect driver.
                self._cb_record_failure(name)
                raise RuntimeError(f"MCP server '{name}' reconnect produced no session")
            # Finding-13 semantics: clear only the open-circuit deadline.
            self._circuit_open_until.pop(name, None)
            return state.session

    def _make_static_notification_handler(self, name: str) -> Any:
        """Build the per-server notification handler for a static session.

        The handler itself NEVER awaits a request on the session: the SDK
        awaits message handlers inline in its receive loop, so an in-handler
        request on the same session can never receive its response — the
        response can only be routed by the receive loop that is parked
        awaiting this handler, and while it is parked EVERY in-flight and
        subsequent call on this shared per-node session stalls with it.
        List-change refreshes are debounced per (server, kind), coalesced
        per (server, kind), and SPAWNED as tracked tasks
        (:meth:`_run_static_notification_refresh`), mirroring the pool
        handler (:meth:`_make_pool_notification_handler`).

        Deliberate change from the pre-#839 handler: the error pill
        (``_last_error``) is no longer cleared on ANY incoming
        notification — only a COMPLETED refresh (push, periodic, or
        reconnect-driven) clears it, because a notification's arrival
        proves nothing about whether the previous refresh failure
        resolved. A transient push-refresh failure can therefore show in
        the pill until the next refresh attempt (worst case: the periodic
        pass), where the old handler cleared it on the next notification.
        """

        async def _on_notification(
            msg: Any,  # RequestResponder | ServerNotification | Exception
        ) -> None:
            if not isinstance(msg, mcp_types.ServerNotification):
                return
            kind = _LIST_CHANGED_KINDS.get(type(msg.root))
            if kind is None:
                return
            marker = (name, kind)
            # Debounce per (server, kind): refreshes are kind-scoped, so a
            # server-scoped stamp would DROP a different-kind notification
            # landing inside the window (tools push swallowing the prompts
            # push 100ms behind it) with no parked runner to observe it —
            # nothing would refresh prompts until the server pushed again.
            now = time.monotonic()
            last = self._last_notification_refresh.get(marker, 0.0)
            if now - last < self._NOTIFICATION_DEBOUNCE:
                log.debug(
                    "Debouncing %s notification from '%s' (%.1fs since last refresh)",
                    kind,
                    name,
                    now - last,
                )
                return
            if marker in self._static_refresh_pending:
                # A runner for this server+kind is queued but has not yet
                # issued its list call — it will observe this change when
                # it runs. Skipping bounds the connect lock's waiter queue
                # at one parked runner per server+kind, so a notifying-
                # but-slow server cannot accrete waiters that starve the
                # reconnect drivers sharing that lock.
                log.debug(
                    "Coalescing static %s notification from '%s' (refresh queued)",
                    kind,
                    name,
                )
                return
            try:
                # SPAWNED, never awaited — see the factory docstring: an
                # inline await here wedges the receive loop permanently.
                # The refresh runs as its own tracked task.
                #
                # Bound at dispatch time (not a module-level name table)
                # so instance-level overrides keep working and mypy
                # checks the attribute references.
                refreshers: dict[str, Callable[[str], Awaitable[Any]]] = {
                    "tools": self._refresh_server_tools,
                    "resources": self._refresh_server_resources,
                    "prompts": self._refresh_server_prompts,
                }
                log.info("Received %s/list_changed from '%s'", kind, name)
                self._last_notification_refresh[marker] = now
                self._static_refresh_pending.add(marker)
                self._spawn_background(
                    self._run_static_notification_refresh(name, kind, refreshers[kind]),
                    f"static {kind} refresh for '{name}'",
                )
            except Exception as exc:
                # Scheduling failed (loop shutting down) — release the
                # coalesce marker or this server+kind never refreshes
                # again. Structured fields only — ``exc_info=True`` would
                # serialize the chained ``httpx.Request`` whose headers
                # carry the configured bearer for ``auth_type=static``.
                self._static_refresh_pending.discard(marker)
                log.warning(
                    "Static refresh scheduling failed server=%s exc=%s",
                    name,
                    type(exc).__name__,
                )

        return _on_notification

    async def _run_static_notification_refresh(
        self,
        name: str,
        kind: str,
        refresh: Callable[[str], Awaitable[Any]],
    ) -> None:
        """Body of a spawned static ``list_changed`` refresh — never on the receive loop.

        The static-path port of :meth:`_run_notification_refresh` — see that
        docstring for the full protocol rationale (serialize-then-list so the
        last publish is always the freshest; coalesce marker cleared at
        lock-ACQUIRE so a change the in-flight list missed spawns exactly one
        successor; debounce stamp SURVIVES failure and is popped by every
        teardown; ``finally`` discard gated on non-acquisition so it never
        clobbers a successor's marker; bounded wedged-notifier residual).
        The static primitives differ:

        * Serialization is on the per-name CONNECT lock — the static path's
          one writer lock, shared with ``_connect_one_locked``'s discovery
          wiring, ``_refresh_server``, and the teardown protocol.
        * The remove → re-add race is closed by LOCK IDENTITY (the pool uses
          entry identity): ``remove_server_sync`` retires the lock object
          after teardown, so a runner that parked on the OLD lock must not
          touch state now owned by a NEW-lock holder.
        * ``(Exception, BaseExceptionGroup)`` is caught for the same
          hygiene reason as the pool runner, but the credential at stake
          is the CONFIGURED bearer: for ``auth_type=static`` servers the
          chained ``httpx.Request`` headers carry it, and an escaping
          exception would reach ``_spawn_background``'s ``exc_info`` log.
          The log line and error pill carry ``type: str(exc)`` — the
          message text (server error / URL) is diagnostic and header-free;
          it is ``exc_info``'s serialized request CHAIN that leaks, so
          that is the only thing withheld.

        Bounded contention residual, accepted: this runner (and the
        lock-serialized :meth:`_refresh_server`) holds the connect lock
        for up to one list timeout (``_CONNECT_TIMEOUT``), and a
        dispatch-driven reconnect queues behind it — but a reconnect only
        runs after the session was EVICTED, and every parked runner bails
        instantly on the session check below once that happens, so the
        added wait is at most the single in-flight list call. A caller
        timeout expiring on that wait is deliberately not a breaker
        record (see :meth:`_cb_auto_reconnect`); the next dispatch
        retries.
        """
        marker = (name, kind)
        lock = self._static_connect_locks.get(name)
        if lock is None:
            # Removed (lock retired) between spawn and run — nothing to
            # refresh; the marker is still ours to release. ``.get()``, not
            # the get-or-create helper: minting a fresh lock here would
            # resurrect an entry for a server that no longer exists.
            self._static_refresh_pending.discard(marker)
            return
        acquired = False
        try:
            async with lock:
                self._static_refresh_pending.discard(marker)
                acquired = True
                if self._static_connect_locks.get(name) is not lock:
                    # Removed (and possibly re-added) while we were parked:
                    # the notification belonged to the old transport, and a
                    # re-add publishes its own discovery under the NEW lock.
                    return
                state = self._static_servers.get(name)
                if state is None or state.session is None:
                    # Torn down / evicted while we were parked. The
                    # reconnect's rediscovery republishes, and every
                    # teardown pops the debounce stamp, so the reconnected
                    # transport's first notification refreshes immediately.
                    return
                await refresh(name)
                self._last_error.pop(name, None)
        except (Exception, BaseExceptionGroup) as exc:
            # ``type: str(exc)``, never ``exc_info`` — the message text is
            # diagnostic and header-free; it is the serialized exception
            # CHAIN (chained ``httpx.Request`` whose headers carry the
            # configured bearer for ``auth_type=static``) that must never
            # reach the log.
            log.warning(
                "Static %s refresh after notification failed for '%s' exc=%s: %s",
                kind,
                name,
                type(exc).__name__,
                exc,
            )
            self._set_error(name, f"Refresh failed: {type(exc).__name__}: {exc}")
        finally:
            if not acquired:
                # Cancelled (or failed) while PARKED — the marker still in
                # the set is OURS; release it or this server+kind never
                # refreshes again. After the at-acquire discard, a marker
                # present at exit belongs to the SUCCESSOR spawned during
                # our in-flight list — discarding it would let the handler
                # mint runners past the one-parked-runner bound.
                self._static_refresh_pending.discard(marker)

    async def _static_transport_owner(
        self,
        name: str,
        cfg: dict[str, Any],
        ready: asyncio.Future[Any],
        close_requested: asyncio.Event,
    ) -> None:
        """Own the transport + session cm lifecycle for one static server.

        THE anyio host-task invariant, and the fix for the flaky-server
        100%-CPU regression: anyio cancel scopes (inside the SDK's
        ``streamablehttp_client`` / ``stdio_client`` / ``ClientSession`` task
        groups) are bound to the task that enters them. A scope whose host
        task has finished can never be exited, and once such a scope is
        cancelled — by anyio's own ``task_done`` when a transport child dies
        with the server, or by ``ClientSession.__aexit__`` during a later
        teardown — anyio re-delivers cancellation to it in a ``call_soon``
        loop every event-loop iteration, forever (~10^5+ callbacks/s per
        scope, verified against anyio 4.14.1; no upstream fix). Entering the
        cms from short-lived connect tasks (every health tick is a new task)
        made that state routine.

        So this long-lived task IS the cm host: it enters the transport and
        ``ClientSession`` contexts, initializes (each phase bounded by a
        SAME-TASK ``asyncio.timeout``, whose cancel unwinds the async-with
        chain in-task and completes), reports readiness, then parks. It
        unwinds — again in-task — on any of:

          * a requested close (``close_requested`` set by the teardown
            protocol in :meth:`_teardown_static_session`);
          * ONE ``cancel()`` (teardown escalation, or shutdown);
          * the transport task group collapsing underneath it because the
            server died — anyio cancels the scope, finds THIS task alive and
            parked, and the delivery drains normally instead of spinning.

        The exit stack is deliberately a local: nothing outside this task can
        reach it to ``aclose()`` cross-task. Connect-phase failures are
        delivered through *ready*; post-ready death is observed by the
        done-callback (:meth:`_on_static_owner_death`), which evicts the
        session for the health loop / next dispatch to reconnect.
        """
        state = self._ensure_static_state(name)
        try:
            async with AsyncExitStack() as stack:
                transport = cfg.get("type", "stdio")
                if transport in ("http", "streamable-http") or "url" in cfg:
                    async with asyncio.timeout(self._CONNECT_TIMEOUT):
                        read, write, _ = await stack.enter_async_context(
                            streamablehttp_client(url=cfg["url"], headers=cfg.get("headers"))
                        )
                else:
                    from turnstone.core.env import scrubbed_env

                    env = scrubbed_env(extra=cfg.get("env", {}))
                    params = StdioServerParameters(
                        command=cfg.get("command", ""),
                        args=cfg.get("args", []),
                        env=env,
                    )
                    async with asyncio.timeout(self._CONNECT_TIMEOUT):
                        read, write = await stack.enter_async_context(stdio_client(params))
                # Stash stream refs so _pre_close_streams can unblock the
                # SDK's transport tasks promptly during teardown (SDK #2147).
                state.streams = (read, write)
                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        message_handler=self._make_static_notification_handler(name),
                    )
                )
                async with asyncio.timeout(self._CONNECT_TIMEOUT):
                    await session.initialize()
                if not ready.done():
                    ready.set_result(session)
                await close_requested.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.set_exception(ConnectionError(f"MCP connect for '{name}' was cancelled"))
                with contextlib.suppress(BaseException):
                    ready.exception()  # mark retrieved: the waiter may be gone
            raise
        except (BaseExceptionGroup, Exception) as exc:
            if not ready.done():
                # Connect-phase failure: deliver to the waiting caller. Phase
                # timeouts arrive as TimeoutError (``asyncio.timeout`` converts
                # its own cancel in THIS task); transport task-group failures
                # may be BaseExceptionGroup — passed through, the consumers
                # handle groups explicitly.
                ready.set_exception(exc)
                with contextlib.suppress(BaseException):
                    ready.exception()  # mark retrieved: the waiter may be gone
                return
            # Post-ready death: the server dropped the transport under a live
            # session. The done-callback evicts; the health loop owns the
            # reconnect story. Quiet — a flapping server would otherwise spam.
            log.debug("MCP transport owner for '%s' terminated: %r", name, exc)
        finally:
            # BaseExceptions outside the arms above (interpreter exits, or a
            # library's BaseException-derived control-flow escape) keep
            # unwinding this task — but must still resolve the waiter, or the
            # connecting caller blocks until its outer bound (and
            # _connect_all's initial connect has none). For SystemExit /
            # KeyboardInterrupt asyncio additionally stops the loop right
            # after, making the delivery moot there — it is load-bearing for
            # every other BaseException shape, and free for the exits.
            if not ready.done():
                ready.set_exception(
                    ConnectionError(f"MCP transport owner for '{name}' exited during connect")
                )
                with contextlib.suppress(BaseException):
                    ready.exception()  # mark retrieved: the waiter may be gone

    def _on_static_owner_death(self, name: str, task: asyncio.Task[None]) -> None:
        """Done-callback for a transport owner: observe UNREQUESTED death.

        A requested teardown de-registers the owner from state before closing
        it, so reaching here with ``state.owner_task is task`` means the
        transport collapsed underneath a live session (server died, stream
        dropped) — the owner unwound its scopes in-task (its job) and the
        session is now a corpse. Evict it so dispatch fails fast into
        reconnect and the health loop picks it up on its next tick, instead
        of every caller rediscovering the corpse via a dead-transport error.
        The breaker is left alone: no dispatch failure was observed, and
        connect outcomes are recorded by ``_ensure_static_connected``.
        """
        with contextlib.suppress(BaseException):
            if not task.cancelled():
                task.exception()  # mark retrieved; the owner already logged
        state = self._static_servers.get(name)
        if state is None or state.owner_task is not task:
            return
        state.owner_task = None
        state.close_requested = None
        if state.session is not None:
            self._drop_static_session_and_stamp(name, state)
            log.info("MCP server '%s' transport terminated; session evicted for reconnect", name)

    async def _connect_one_locked(self, name: str, cfg: dict[str, Any]) -> None:
        """Connect to a single MCP server and discover its tools.

        MUST run under the per-server connect lock (see :meth:`_connect_one`).

        The transport + session cms are entered by a dedicated OWNER task
        (:meth:`_static_transport_owner`); this method only WAITS for it to
        report readiness — so a caller-side cancellation (the attempt timeout
        in ``_ensure_static_connected``, shutdown, a sync boundary giving up)
        can never abandon an anyio cancel scope mid-exit. On failure the owner
        unwinds fully in its own task. This method's job is bookkeeping:
        the close protocol on the way in (stale-guard), state fields and
        catalog discovery on the way out.
        """
        # Operate on a single state object throughout: get-or-create up front
        # so the stale-entry guard and the post-handshake field assignments
        # touch the same instance (PR #296 invariant 5: identity stability).
        state = self._ensure_static_state(name)

        # Guard: close any stale owner/session so we don't leak. Transport
        # errors in the sync dispatch methods evict the session but leave the
        # owner (and its open transport) behind; the close protocol clears
        # both (and is a no-op on a brand new entry).
        await self._teardown_static_session(name)

        transport = cfg.get("type", "stdio")
        if transport in ("http", "streamable-http") or "url" in cfg:
            # Pre-flight TCP check: fail fast before spawning an owner and
            # entering the SDK's anyio task group at all (an ECONNREFUSED
            # surfaces here as a clean ConnectionError instead of a
            # cancel-scope unwind).
            await self._tcp_probe(name, cfg["url"])
        elif not cfg.get("command", ""):
            # Default transport is stdio; without a command there is nothing
            # to spawn. Preserves the historical no-session no-raise shape
            # (``_ensure_static_connected`` converts it to a clean failure).
            log.warning("MCP server '%s' has no command configured", name)
            return

        loop = asyncio.get_running_loop()
        ready: asyncio.Future[Any] = loop.create_future()
        close_requested = asyncio.Event()
        owner = asyncio.create_task(
            self._static_transport_owner(name, cfg, ready, close_requested),
            name=f"mcp-transport-owner:{name}",
        )
        owner.add_done_callback(lambda t: self._on_static_owner_death(name, t))
        try:
            session = await ready
        except asyncio.CancelledError:
            # Caller cancelled while the owner was still connecting. Close the
            # owner with the one-cancel protocol and let its unwind finish
            # in-task; never abandon it mid-exit, never cancel twice.
            close_requested.set()
            if not owner.done():
                owner.cancel()
                await asyncio.wait({owner}, timeout=self._OWNER_CANCEL_GRACE_S)
            raise
        except BaseException:
            # Connect failed: the owner delivered the failure and is unwinding
            # itself in-task. Reap quietly, then surface the error.
            await asyncio.wait({owner}, timeout=self._OWNER_CLOSE_GRACE_S)
            raise

        if owner.done():
            # The transport collapsed in the instant between readiness and the
            # caller resuming (flapping server). Treat it as a failed connect
            # rather than installing a corpse the first dispatch trips over.
            raise ConnectionError(f"MCP server '{name}' transport died during connect")

        state.owner_task = owner
        state.close_requested = close_requested
        state.session = session

        # Check push notification support for each capability
        caps = session.get_server_capabilities()

        tools_cap = getattr(caps, "tools", None) if caps else None
        state.supports_list_changed = bool(getattr(tools_cap, "listChanged", False))

        resources_cap = getattr(caps, "resources", None) if caps else None
        state.supports_resources = resources_cap is not None
        state.supports_resource_list_changed = bool(getattr(resources_cap, "listChanged", False))

        prompts_cap = getattr(caps, "prompts", None) if caps else None
        state.supports_prompts = prompts_cap is not None
        state.supports_prompt_list_changed = bool(getattr(prompts_cap, "listChanged", False))

        # Discover tools. Discovery runs in THIS caller task while the
        # transport is hosted by the owner, so a transport collapse
        # mid-discovery cancels the OWNER, not us — ``_await_owner_discovery``
        # races the owner so that death surfaces as a prompt ConnectionError
        # instead of hanging to the caller-side attempt timeout.
        result = await self._await_owner_discovery(owner, session.list_tools())
        capped = _cap_server_tools(name, result.tools)
        server_tools: list[dict[str, Any]] = [_mcp_to_openai(name, tool) for tool in capped]

        state.tools = server_tools
        self._rebuild_tools()

        # Discover resources
        resource_count = 0
        if resources_cap is not None:
            server_resources: list[dict[str, Any]] = []
            res_result = await self._await_owner_discovery(owner, session.list_resources())
            for r in res_result.resources:
                server_resources.append(
                    {
                        "uri": str(r.uri),
                        "name": r.name or "",
                        "description": r.description or "",
                        "mimeType": r.mimeType or "",
                        "server": name,
                    }
                )
            # Also include resource templates (catalog-only — not directly
            # readable via read_resource since they contain URI placeholders)
            tmpl_result = await self._await_owner_discovery(
                owner, session.list_resource_templates()
            )
            for t in tmpl_result.resourceTemplates:
                server_resources.append(
                    {
                        "uri": str(t.uriTemplate),
                        "name": t.name or "",
                        "description": t.description or "",
                        "mimeType": t.mimeType or "",
                        "server": name,
                        "template": True,
                    }
                )
            resource_count = len(server_resources)
            state.resources = server_resources
            self._rebuild_resources()

        # Discover prompts
        prompt_count = 0
        if prompts_cap is not None:
            server_prompts: list[dict[str, Any]] = []
            prompt_result = await self._await_owner_discovery(owner, session.list_prompts())
            for p in prompt_result.prompts:
                server_prompts.append(
                    {
                        "name": f"mcp__{name}__{p.name}",
                        "original_name": p.name,
                        "server": name,
                        "description": p.description or "",
                        "arguments": [
                            {
                                "name": a.name,
                                "description": a.description or "",
                                "required": a.required or False,
                            }
                            for a in (p.arguments or [])
                        ],
                    }
                )
            prompt_count = len(server_prompts)
            state.prompts = server_prompts
            self._rebuild_prompts()

        push_parts: list[str] = []
        if state.supports_list_changed:
            push_parts.append("tools")
        if state.supports_resource_list_changed:
            push_parts.append("resources")
        if state.supports_prompt_list_changed:
            push_parts.append("prompts")
        push_status = f" (push: {','.join(push_parts)})" if push_parts else ""
        log.info(
            "Connected MCP server '%s' — %d tool(s), %d resource(s), %d prompt(s)%s",
            name,
            len(result.tools),
            resource_count,
            prompt_count,
            push_status,
        )

        # Sync discovered prompts into governance storage
        try:
            self.sync_prompts_to_storage()
        except Exception:
            log.warning("Prompt sync after connect failed for '%s'", name, exc_info=True)

        # Connection succeeded — clear any previous error
        self._last_error.pop(name, None)

    def _make_pool_notification_handler(self, key: tuple[str, str]) -> Any:
        """Build the per-(user, server) notification handler for a pool session.

        Bound to ``(user_id, server_name)`` via closure so a push-driven
        ``*/list_changed`` only refreshes THIS user's catalog, never the static
        path's — calling the static ``_refresh_server_tools(server_name)`` from
        a pool session would mutate ``_static_servers[server_name].tools`` and
        ``_tool_map``, broadcasting one user's tool view to every other session.
        Body unchanged from the pre-owner-task closure in ``_connect_one_pool``;
        extracted because the session cm is now entered by the transport owner
        (mirrors ``_make_static_notification_handler``).
        """
        user_id, server_name = key

        async def _on_pool_notification(
            msg: Any,  # RequestResponder | ServerNotification | Exception
        ) -> None:
            if not isinstance(msg, mcp_types.ServerNotification):
                return
            kind = _LIST_CHANGED_KINDS.get(type(msg.root))
            if kind is None:
                return
            marker = (key, kind)
            # Debounce per (key, kind) — see the static handler's rationale:
            # refreshes are kind-scoped, so a key-scoped stamp would drop a
            # different-kind notification inside the window outright.
            now = time.monotonic()
            last = self._last_pool_notification_refresh.get(marker, 0.0)
            if now - last < self._NOTIFICATION_DEBOUNCE:
                log.debug(
                    "Debouncing pool %s notification user=%s server=%s (%.1fs since last refresh)",
                    kind,
                    user_id,
                    server_name,
                    now - last,
                )
                return
            if marker in self._pool_refresh_pending:
                # A runner for this key+kind is queued but has not yet
                # issued its list call — it will observe this change
                # when it runs. Skipping bounds ``open_lock``'s waiter
                # queue at one parked runner per key+kind, so a
                # notifying-but-slow server cannot accrete waiters that
                # starve same-key dispatches and idle eviction.
                log.debug(
                    "Coalescing pool %s notification user=%s server=%s (refresh queued)",
                    kind,
                    user_id,
                    server_name,
                )
                return
            try:
                # SPAWNED, never awaited: the SDK awaits notification
                # handlers inline in its receive loop, so a handler that
                # awaits a request on the SAME session deadlocks — the
                # response can never be routed while the handler is
                # parked, every in-flight call on the session stalls
                # with it, and the refresh only ever exits via its
                # timeout. The refresh runs as its own tracked task.
                #
                # Bound at dispatch time (not a module-level name table)
                # so instance-level overrides keep working and mypy
                # checks the attribute references.
                refreshers: dict[
                    str,
                    Callable[[tuple[str, str]], Awaitable[tuple[list[str], list[str]]]],
                ] = {
                    "tools": self._refresh_pool_server_tools,
                    "resources": self._refresh_pool_server_resources,
                    "prompts": self._refresh_pool_server_prompts,
                }
                log.info(
                    "Received %s/list_changed from pool user=%s server=%s",
                    kind,
                    user_id,
                    server_name,
                )
                self._last_pool_notification_refresh[marker] = now
                self._pool_refresh_pending.add(marker)
                self._spawn_background(
                    self._run_notification_refresh(key, kind, refreshers[kind]),
                    f"pool {kind} refresh for '{server_name}'",
                )
            except Exception as exc:
                # Scheduling failed (loop shutting down) — release the
                # coalesce marker or this key+kind never refreshes again.
                # Structured fields only — ``exc_info=True`` would
                # serialize the chained ``httpx.Request`` whose headers
                # carry ``Authorization: Bearer <token>``.
                self._pool_refresh_pending.discard(marker)
                log.warning(
                    "Pool refresh scheduling failed user=%s server=%s exc=%s",
                    user_id,
                    server_name,
                    type(exc).__name__,
                )

        return _on_pool_notification

    async def _run_notification_refresh(
        self,
        key: tuple[str, str],
        kind: str,
        refresh: Callable[[tuple[str, str]], Awaitable[tuple[list[str], list[str]]]],
    ) -> None:
        """Body of a spawned ``list_changed`` refresh — never on the receive loop.

        Serializes on ``open_lock`` before refreshing. An unserialized
        refresh races two writers: an in-flight ``_connect_one_pool``,
        whose final wiring block would overwrite the refresh's newer
        catalog with its older discovery snapshot; and a sibling
        refresh for the same key, where the slower list call can
        publish the older catalog last. Under the lock each refresh
        issues its list call only after the previous publisher
        finished, so the last publish is always the freshest.

        The coalesce marker (set by the handler at spawn) is cleared
        the moment the lock is ACQUIRED, before the list call: a
        notification landing during the in-flight list may announce a
        change that list already missed, so it must spawn exactly one
        successor — which parks behind this lock. Together with the
        handler's marker check this bounds the waiter queue at one
        parked runner per key+kind: a same-key dispatch waits at most
        two refresh timeouts, not an unbounded runner FIFO. The
        ``finally`` discard covers cancellation while parked and is
        GATED on non-acquisition: after the at-acquire discard, a
        marker present at exit belongs to the successor spawned during
        our list call, and clobbering it would re-open the unbounded
        FIFO the marker exists to prevent.

        The same-entry recheck under the lock discards a refresh whose
        entry was replaced (full drop + re-create) while it waited —
        the notification belonged to the old transport and the
        replacement published its own discovery. A session evicted
        while we were parked returns quietly for the same reason:
        the reconnect's discovery republishes, and every teardown path
        pops the debounce stamp, so the reconnected transport's first
        notification refreshes immediately.

        The debounce stamp (set at schedule time) deliberately SURVIVES
        a failed refresh: popping it re-armed the handler on every
        notification, so a fast-failing server spawned refresh tasks at
        its notification rate. Keeping it caps attempts at one per
        debounce window; a change announced during the remainder of a
        failed window converges on the server's next ``list_changed``
        or the entry's next reconnect.

        ``BaseExceptionGroup`` is caught alongside ``Exception``: a
        wedged anyio transport surfaces session-op failures as groups,
        and a group escaping this frame would reach
        ``_spawn_background``'s failure log, whose ``exc_info``
        serializes the chained ``httpx.Request`` — headers carrying
        ``Authorization: Bearer <token>``.

        Bounded residual, accepted: a server that keeps notifying while
        every list call hangs to ``_CONNECT_TIMEOUT`` keeps THIS
        entry's ``open_lock`` near-continuously occupied (one active +
        one parked runner), deferring idle eviction of the entry (the
        eviction pass skips a contested lock). The churn ends at the
        first real dispatch (whose transport failure evicts the
        session, after which parked runners bail on the session check),
        at server recovery, or when the notifications stop; other
        entries are unaffected (per-entry lock).
        """
        user_id, server_name = key
        marker = (key, kind)
        entry = self._user_pool_entries.get(key)
        if entry is None:
            self._pool_refresh_pending.discard(marker)
            return
        acquired = False
        try:
            async with entry.open_lock:
                self._pool_refresh_pending.discard(marker)
                acquired = True
                if self._user_pool_entries.get(key) is not entry:
                    return
                if entry.session is None:
                    return
                await refresh(key)
        except (Exception, BaseExceptionGroup) as exc:
            # Structured fields only — ``exc_info`` would serialize
            # the chained ``httpx.Request`` whose headers carry
            # ``Authorization: Bearer <token>``.
            log.warning(
                "Pool %s refresh after notification failed user=%s server=%s exc=%s",
                kind,
                user_id,
                server_name,
                type(exc).__name__,
            )
        finally:
            if not acquired:
                # Cancelled (or failed) while PARKED — the marker still
                # in the set is OURS; release it or the key+kind never
                # refreshes again. After the at-acquire discard, a
                # marker present at exit belongs to the SUCCESSOR
                # spawned during our in-flight list — discarding it
                # would let the handler mint runners past the
                # one-parked-runner bound.
                self._pool_refresh_pending.discard(marker)

    async def _pool_transport_owner(
        self,
        key: tuple[str, str],
        client_kwargs: dict[str, Any],
        ready: asyncio.Future[Any],
        close_requested: asyncio.Event,
    ) -> None:
        """Own the transport + session cm lifecycle for one pool entry.

        The pool sibling of :meth:`_static_transport_owner` — see that method
        for the full anyio host-task rationale (a cancel scope whose host task
        has finished can never be exited, and anyio then re-delivers
        cancellation to it in a ``call_soon`` loop forever, the SDK #2147
        100%-CPU spin). The invariant is the same: this ONE long-lived task
        enters the transport + ``ClientSession`` cms, initializes, reports
        readiness, parks, and unwinds — always in-task.

        The caller builds ``client_kwargs`` (url + merged bearer headers, plus
        the auth-capture ``httpx_client_factory`` when a carrier is active) and
        runs the TCP pre-flight, so this task stays dumb. Connect-phase failures
        are delivered through *ready* (its cancel unwinds the async-with chain
        in-task); post-ready death is observed by the done-callback
        (:meth:`_on_pool_owner_death`), which evicts the session for the next
        dispatch to reconnect. The exit stack is a local so nothing outside this
        task can reach it to ``aclose()`` cross-task.
        """
        user_id, server_name = key
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(self._CONNECT_TIMEOUT):
                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(**client_kwargs)
                    )
                # Stash stream refs so _pre_close_streams can unblock the SDK's
                # transport tasks promptly during teardown (SDK #2147).
                entry = self._user_pool_entries.get(key)
                if entry is not None:
                    entry.streams = (read, write)
                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        message_handler=self._make_pool_notification_handler(key),
                    )
                )
                async with asyncio.timeout(self._CONNECT_TIMEOUT):
                    await session.initialize()
                if not ready.done():
                    ready.set_result(session)
                await close_requested.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.set_exception(
                    ConnectionError(
                        f"MCP pool connect for '{server_name}' (user={user_id}) was cancelled"
                    )
                )
                with contextlib.suppress(BaseException):
                    ready.exception()  # mark retrieved: the waiter may be gone
            raise
        except (BaseExceptionGroup, Exception) as exc:
            if not ready.done():
                # Connect-phase failure: deliver to the waiting caller. Phase
                # timeouts arrive as TimeoutError; transport task-group failures
                # may be BaseExceptionGroup — passed through, the consumers
                # handle groups explicitly.
                ready.set_exception(exc)
                with contextlib.suppress(BaseException):
                    ready.exception()  # mark retrieved: the waiter may be gone
                return
            # Post-ready death: the server dropped the transport under a live
            # session. The done-callback evicts; the next dispatch reconnects.
            log.debug(
                "MCP pool transport owner user=%s server=%s terminated: %r",
                user_id,
                server_name,
                exc,
            )
        finally:
            # BaseExceptions outside the arms above (interpreter exits, or a
            # library's BaseException-derived control-flow escape) keep
            # unwinding this task — but must still resolve the waiter, or the
            # connecting caller blocks until its outer bound. For SystemExit /
            # KeyboardInterrupt asyncio additionally stops the loop right
            # after, making the delivery moot there — it is load-bearing for
            # every other BaseException shape, and free for the exits.
            if not ready.done():
                ready.set_exception(
                    ConnectionError(
                        f"MCP pool transport owner for '{server_name}' "
                        f"(user={user_id}) exited during connect"
                    )
                )
                with contextlib.suppress(BaseException):
                    ready.exception()  # mark retrieved: the waiter may be gone

    async def _teardown_pool_entry(self, key: tuple[str, str]) -> None:
        """Tear down a pool entry's session/transport (the ONE canonical order).

        Shared by :meth:`_connect_one_pool`'s stale-guard,
        :meth:`_close_pool_entry_if_idle`, and shutdown so a future ordering fix
        lands in one place. Does NOT pop the entry from ``_user_pool_entries`` —
        callers own map / catalog cleanup. Safe to call while holding
        ``entry.open_lock`` (it never takes a lock itself). No-op when the entry
        is gone.

        Close protocol (the anyio host-task invariant, shared with
        :meth:`_teardown_static_session`): the transport + ``ClientSession`` cms
        were entered by the entry's OWNER task and can only be exited by it — an
        exit-stack ``aclose()`` from any other task raises anyio's cross-task
        scope-exit error, and a scope whose host task has finished can never
        drain, which arms anyio's ``_deliver_cancellation`` retry into a
        permanent ``call_soon`` storm (the SDK #2147 100%-CPU spin this design
        removes). So:

        1. null the session (concurrent dispatch reads see "disconnected");
        2. pre-close the transport streams (unblocks anyio transport tasks stuck
           on zero-buffer ``send()``);
        3. ask the owner to close (``close_requested.set()``) and give its
           in-task unwind ``_OWNER_CLOSE_GRACE_S``;
        4. escalate to ONE ``cancel()`` and wait ``_OWNER_CANCEL_GRACE_S``;
        5. if it is STILL unwinding, leave it to finish solo (a SECOND cancel
           would abandon a scope exit mid-flight and mint the exact zombie this
           protocol prevents) and run the orphaned-scope disarm sweep as a
           backstop.

        A plain-session entry with no owner (unit-test seeding) short-circuits
        after step 2 — that path stays cheap.
        """
        entry = self._user_pool_entries.get(key)
        if entry is None:
            return
        self._drop_session_and_stamp(key, entry)
        owner = entry.owner_task
        close_requested = entry.close_requested
        entry.owner_task = None
        entry.close_requested = None
        # Signal BEFORE the first await: if this coroutine is itself cancelled
        # mid-teardown, the owner must already have its marching orders — a
        # parked owner that never learns of the close would hold the transport
        # open forever.
        if close_requested is not None:
            close_requested.set()
        await self._pre_close_streams(key)
        if owner is None or owner.done():
            return
        _done, pending = await asyncio.wait({owner}, timeout=self._OWNER_CLOSE_GRACE_S)
        if pending:
            owner.cancel()
            _done, pending = await asyncio.wait({owner}, timeout=self._OWNER_CANCEL_GRACE_S)
        if pending:
            user_id, server_name = key
            log.warning(
                "MCP pool transport owner for user=%s server=%s still unwinding after "
                "close+cancel; leaving it to finish on its own",
                user_id,
                server_name,
            )
            self._maybe_disarm_orphaned_scopes(
                f"slow pool owner unwind for '{server_name}' (user={user_id})"
            )

    def _on_pool_owner_death(self, key: tuple[str, str], task: asyncio.Task[None]) -> None:
        """Done-callback for a pool transport owner: observe UNREQUESTED death.

        A requested teardown de-registers the owner from the entry before
        closing it, so reaching here with ``entry.owner_task is task`` means the
        transport collapsed underneath a live session (server died, stream
        dropped) — the owner unwound its scopes in-task and the session is now a
        corpse. Evict the session so the next dispatch reconnects; the entry and
        its catalog are left in place (evict-session-keep-entry — the next
        connect reuses the discovered catalog). The entry is NOT popped and the
        catalogs are NOT rebuilt here.
        """
        with contextlib.suppress(BaseException):
            if not task.cancelled():
                task.exception()  # mark retrieved; the owner already logged
        entry = self._user_pool_entries.get(key)
        if entry is None or entry.owner_task is not task:
            return
        entry.owner_task = None
        entry.close_requested = None
        had_session = entry.session is not None
        self._drop_session_and_stamp(key, entry)
        if had_session:
            user_id, server_name = key
            log.info(
                "MCP pool transport terminated user=%s server=%s; session evicted for reconnect",
                user_id,
                server_name,
            )

    async def _await_owner_discovery(self, owner: asyncio.Task[None], coro: Any) -> Any:
        """Await a discovery call in the caller task, aborting if the OWNER dies.

        Shared by the static (``_connect_one_locked``) and pool
        (``_connect_one_pool``) connect paths. Discovery (``list_tools`` /
        ``list_resources`` / ``list_prompts``) runs in the connecting caller,
        but the transport servicing it is hosted by ``owner``. When that
        transport collapses — e.g. the SDK tears its task group down on an
        upstream 401 — anyio cancels the OWNER (the scope host), NOT this
        caller, so a bare ``await`` on the session's response stream would hang
        until the surrounding bound fires. Racing the owner turns a transport
        death into a prompt ``ConnectionError`` that the caller's ``except``
        arm converts into a clean teardown. The owner is never cancelled here —
        teardown owns its lifecycle; only the discovery future is reaped.
        """
        call: asyncio.Future[Any] = asyncio.ensure_future(coro)
        try:
            await asyncio.wait({call, owner}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # Caller cancelled (attempt timeout / phase ``asyncio.timeout`` /
            # shutdown): reap the discovery future — gather waits for the
            # cancel to land and absorbs its outcome — leave the owner to
            # teardown, and re-raise ours.
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            raise
        if call.done():
            if call.cancelled():
                # The discovery future was cancelled out from under us (an
                # SDK-internal cancellation shape, not this method's own reap)
                # — the transport can no longer answer, which is the same
                # failure class as the owner dying. Convert instead of leaking
                # a bare CancelledError the caller would misread as its own
                # cancellation.
                raise ConnectionError("MCP discovery request cancelled by transport failure")
            return call.result()  # normal result, or the real discovery error
        # Owner finished first — the transport died under discovery. Reap the
        # discovery future (gather absorbs its cancellation outcome; a caller
        # cancellation arriving DURING the reap propagates instead — honoring
        # the cancel beats reporting the dead transport).
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        raise ConnectionError("MCP transport owner died during discovery")

    async def _connect_one_pool(
        self,
        key: tuple[str, str],
        cfg: dict[str, Any],
        access_token: str,
        *,
        auth_capture: _AuthCapture | None = None,
        auth_fired_event: asyncio.Event | None = None,
    ) -> PoolEntryState:
        """Connect a single per-(user, server) pool entry.

        Mirror of :meth:`_connect_one` for ``auth_type='oauth_user'``,
        with three differences:

        * Streamable-HTTP transport only — pool servers are remote.
        * ``Authorization: Bearer {access_token}`` injected into headers
          alongside any operator-supplied static headers.
        * Tool / resource / prompt catalog discovery runs after
          ``initialize()`` (RFC §3.2 — discovery covers all three
          catalogs, capability-gated). The notification handler is
          bound to ``(user_id, server_name)`` so push-driven
          ``*/list_changed`` updates only refresh the owning user's
          catalog (the static refreshers must NEVER fire from a pool
          session — they would clobber static-path state).

        When ``auth_capture`` is supplied, the underlying ``httpx``
        client is built via a factory whose response hook records 401/403
        status + ``WWW-Authenticate`` into the carrier, recovering the
        upstream auth signal that the SDK's ``post_writer`` would
        otherwise swallow. Static-path callers
        (:meth:`_connect_one`) MUST NOT pass this — the static path
        must remain byte-identical, which means the SDK's default
        ``create_mcp_http_client`` factory.

        MUST run on the mcp-loop. Caller holds ``entry.open_lock``.
        """
        user_id, server_name = key
        if "__" in server_name:
            raise RuntimeError(
                f"MCP server name '{server_name}' contains '__' (reserved delimiter)"
            )

        entry = await self._ensure_pool_entry(key)

        # Guard: close any stale owner/session so we don't leak, the same way
        # ``_connect_one_locked`` does for the static path (cf. PR #296
        # invariant 5). The close protocol clears both the owner (and its open
        # transport) and the session, and is a no-op on a brand-new entry.
        await self._teardown_pool_entry(key)

        url = cfg.get("url")
        if not url or cfg.get("type") not in ("http", "streamable-http"):
            raise RuntimeError(
                f"MCP server '{server_name}' requires streamable-http transport for "
                f"auth_type=oauth_user (got transport={cfg.get('type')!r})"
            )

        # Defense-in-depth: reject http:// (non-loopback) before the bearer
        # is attached. _dispatch_pool already screens this at the structured-
        # error boundary; this catch is for any callers that bypass it.
        _validate_oauth_user_url(url)

        # Merge operator-supplied headers with the per-user bearer.
        headers: dict[str, str] = dict(cfg.get("headers") or {})
        headers["Authorization"] = f"Bearer {access_token}"

        client_kwargs: dict[str, Any] = {"url": url, "headers": headers}
        if auth_capture is not None:
            client_kwargs["httpx_client_factory"] = _make_capturing_http_factory(
                auth_capture, fired_event=auth_fired_event
            )

        # Pre-flight TCP check: fail fast before spawning an owner and entering
        # the SDK's anyio task group at all (an ECONNREFUSED surfaces here as a
        # clean ConnectionError instead of a cancel-scope unwind).
        await self._tcp_probe(key, url)

        # The transport + session cms are entered by a dedicated OWNER task
        # (:meth:`_pool_transport_owner`); this method only WAITS for it to
        # report readiness, so a caller-side cancellation (an eviction giving
        # up, shutdown, a sync boundary timing out) can never abandon an anyio
        # cancel scope mid-exit. On failure the owner unwinds fully in its own
        # task. ``test_integration_pool_reuse_401_refresh_and_retry_succeeds``
        # is the historical symptom sentinel for cross-task anyio state on this
        # exact path.
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[Any] = loop.create_future()
        close_requested = asyncio.Event()
        owner = asyncio.create_task(
            self._pool_transport_owner(key, client_kwargs, ready, close_requested),
            name=f"mcp-pool-owner:{user_id}:{server_name}",
        )
        owner.add_done_callback(lambda t: self._on_pool_owner_death(key, t))
        try:
            session = await ready
        except asyncio.CancelledError:
            # Caller cancelled while the owner was still connecting. Close the
            # owner with the one-cancel protocol and let its unwind finish
            # in-task; never abandon it mid-exit, never cancel twice.
            close_requested.set()
            if not owner.done():
                owner.cancel()
                await asyncio.wait({owner}, timeout=self._OWNER_CANCEL_GRACE_S)
            raise
        except BaseException:
            # Connect failed: the owner delivered the failure and is unwinding
            # itself in-task. Reap quietly, then surface the error.
            await asyncio.wait({owner}, timeout=self._OWNER_CLOSE_GRACE_S)
            raise

        if owner.done():
            # The transport collapsed in the instant between readiness and this
            # caller resuming (flapping server). Treat it as a failed connect
            # rather than installing a corpse the first dispatch trips over.
            raise ConnectionError(f"MCP pool server '{server_name}' transport died during connect")

        entry.owner_task = owner
        entry.close_requested = close_requested
        entry.session = session

        # Capability fetch — mirrors the static path's post-ready fetch in
        # ``_connect_one_locked``.
        # Per RFC §3.2 the pool path now discovers resources & prompts too,
        # capability-gated so a server that doesn't implement them stays
        # cheap (no extra round-trips). Capabilities are populated by the
        # initialize roundtrip and immutable thereafter (R13).
        caps = session.get_server_capabilities()
        resources_cap = getattr(caps, "resources", None) if caps else None
        prompts_cap = getattr(caps, "prompts", None) if caps else None
        entry.supports_resources = resources_cap is not None
        entry.supports_resource_list_changed = bool(getattr(resources_cap, "listChanged", False))
        entry.supports_prompts = prompts_cap is not None
        entry.supports_prompt_list_changed = bool(getattr(prompts_cap, "listChanged", False))

        # Discover this user's tool catalog. Discovery runs in THIS caller task
        # while the transport is hosted by the owner, so an upstream failure
        # (e.g. a 401 the SDK surfaces by collapsing its task group) cancels the
        # OWNER, not us — ``_await_owner_discovery`` races the owner so that death
        # aborts discovery promptly instead of hanging on the response stream
        # until the phase timeout. The carrier-race shape used by
        # ``_dispatch_pool_with_entry`` defends a different scenario
        # (reused-session 401 from inside a SECOND dispatch) that doesn't apply
        # to first-connect discovery.
        #
        # ``asyncio.timeout``, NOT ``asyncio.wait_for`` (per
        # ``feedback_asyncio_timeout_vs_wait_for.md``): ``wait_for`` wraps the
        # inner coroutine in a fresh task, so any await that traverses anyio
        # cleanup would run from a different task than entered the scope. That
        # invariant STILL HOLDS here even though the transport cms now live in
        # the owner task — the ``list_tools`` / ``list_resources`` /
        # ``list_resource_templates`` / ``list_prompts`` calls all traverse the
        # same ``mcp/shared/session.py`` ``send_request`` infrastructure (R6).
        # Discovery-phase failures tear down through ``_teardown_pool_entry``,
        # which closes the owner in-task.
        try:
            async with asyncio.timeout(self._CONNECT_TIMEOUT):
                tools_result = await self._await_owner_discovery(owner, session.list_tools())
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                await self._teardown_pool_entry(key)
                raise
            await self._teardown_pool_entry(key)
            raise TimeoutError(f"Pool discovery failed for '{server_name}'") from None
        except TimeoutError:
            await self._teardown_pool_entry(key)
            raise TimeoutError(f"Pool discovery timed out after {self._CONNECT_TIMEOUT}s") from None
        except Exception:
            await self._teardown_pool_entry(key)
            raise

        capped_tools = _cap_server_tools(server_name, tools_result.tools)
        # STAGED — published to the entry only in the final wiring block
        # below, together with resources/prompts: a mid-discovery
        # failure tears the transport down and must leave the entry's
        # (retained) catalog exactly as it was, never half-updated with
        # the per-user maps still holding the old view.
        server_tools = [_mcp_to_openai(server_name, tool) for tool in capped_tools]

        # Phase 7b — discover resources (capability-gated). Same anyio /
        # ``asyncio.timeout`` invariant as the tool discovery above (R1).
        server_resources: list[dict[str, Any]] = []
        if resources_cap is not None:
            try:
                async with asyncio.timeout(self._CONNECT_TIMEOUT):
                    # 1-RTT (gather) instead of 2 sequential RTTs — both
                    # calls share the same timeout budget and target
                    # disjoint catalogs (resources vs. templates), so
                    # ordering is irrelevant.
                    res_result, tmpl_result = await self._await_owner_discovery(
                        owner,
                        asyncio.gather(
                            session.list_resources(),
                            session.list_resource_templates(),
                        ),
                    )
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    await self._teardown_pool_entry(key)
                    raise
                await self._teardown_pool_entry(key)
                raise TimeoutError(f"Pool resource discovery failed for '{server_name}'") from None
            except TimeoutError:
                await self._teardown_pool_entry(key)
                raise TimeoutError(
                    f"Pool resource discovery timed out after {self._CONNECT_TIMEOUT}s"
                ) from None
            except Exception:
                await self._teardown_pool_entry(key)
                raise

            for r in _cap_server_resources(server_name, res_result.resources):
                server_resources.append(
                    {
                        "uri": str(r.uri),
                        "name": r.name or "",
                        "description": r.description or "",
                        "mimeType": r.mimeType or "",
                        "server": server_name,
                    }
                )
            for t in _cap_server_resource_templates(server_name, tmpl_result.resourceTemplates):
                server_resources.append(
                    {
                        "uri": str(t.uriTemplate),
                        "name": t.name or "",
                        "description": t.description or "",
                        "mimeType": t.mimeType or "",
                        "server": server_name,
                        "template": True,
                    }
                )

        # Phase 7b — discover prompts (capability-gated).
        server_prompts: list[dict[str, Any]] = []
        if prompts_cap is not None:
            try:
                async with asyncio.timeout(self._CONNECT_TIMEOUT):
                    prompt_result = await self._await_owner_discovery(owner, session.list_prompts())
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    await self._teardown_pool_entry(key)
                    raise
                await self._teardown_pool_entry(key)
                raise TimeoutError(f"Pool prompt discovery failed for '{server_name}'") from None
            except TimeoutError:
                await self._teardown_pool_entry(key)
                raise TimeoutError(
                    f"Pool prompt discovery timed out after {self._CONNECT_TIMEOUT}s"
                ) from None
            except Exception:
                await self._teardown_pool_entry(key)
                raise

            for p in _cap_server_prompts(server_name, prompt_result.prompts):
                server_prompts.append(
                    {
                        "name": f"mcp__{server_name}__{p.name}",
                        "original_name": p.name,
                        "server": server_name,
                        "description": p.description or "",
                        "arguments": [
                            {
                                "name": a.name,
                                "description": a.description or "",
                                "required": a.required or False,
                            }
                            for a in (p.arguments or [])
                        ],
                    }
                )

        entry.tools = server_tools
        entry.resources = server_resources if resources_cap is not None else None
        entry.prompts = server_prompts if prompts_cap is not None else None

        # Session + owner were published right after connect-readiness (above),
        # before discovery; the per-user catalog maps are rebuilt LAST so a
        # sync-thread reader never observes a tool whose backing entry isn't
        # fully wired. ``_rebuild_user_tool_map`` is what makes
        # ``is_mcp_tool(name, user_id=U)`` return True for the discovered names,
        # and by the time it runs ``entry.session`` is already live — dispatch
        # also re-fetches its own token and lazy-reconnects on session=None, but
        # ordering catches the race at the source. Same invariant covers
        # resources/prompts (R16).
        entry.bound_token = access_token  # remember the bearer this session carries
        entry.last_used = time.monotonic()
        self._user_pool_last_used[key] = entry.last_used

        # Loop-only mutation; sync-thread readers observe the new
        # catalog atomically via per-user dict-gets on ``_user_tools`` /
        # ``_user_resources`` / ``_user_prompts``. Per-user fan-out
        # ensures another user's session never observes this change.
        self._rebuild_and_notify_user_catalogs(user_id)
        return entry

    # -- pool priming ---------------------------------------------------------

    async def _prime_user_server(
        self, key: tuple[str, str], cfg: dict[str, Any], access_token: str
    ) -> int:
        """Proactively connect a pool entry so its catalog populates into
        ``get_tools(user_id)`` WITHOUT waiting for a tool dispatch.

        ``auth_type='oauth_user'`` tools are per-user and were previously
        discovered ONLY lazily, on first dispatch (``_dispatch_pool_with_entry``
        at the ``session is None`` branch). That creates a chicken-and-egg:
        the model can't emit a call for a tool it can't see, but the tool
        only appears after a call connects the pool — so the per-user
        catalog stays empty and the server is stuck "connecting". This
        connects at a known-good moment (OAuth consent completion), commits
        the catalog and fires the per-user tool/resource/prompt listeners so
        any live :class:`ChatSession` refreshes its tool list.

        Idempotent: a no-op if a dispatch (or an earlier prime) already
        established the session. MUST run on the mcp-loop; takes
        ``entry.open_lock`` exactly like dispatch so it can't race a
        concurrent connect/eviction. Returns the discovered tool count.
        """
        entry = await self._ensure_pool_entry(key)
        async with entry.open_lock:
            if entry.session is not None:
                return len(entry.tools or [])
            fresh = await self._connect_one_pool(
                key,
                cfg,
                access_token,
                auth_capture=entry.auth_capture,
                auth_fired_event=entry.auth_fired_event,
            )
            return len(fresh.tools or [])

    def schedule_prime_user_server(
        self,
        *,
        user_id: str,
        server_name: str,
        access_token: str,
        server_row: dict[str, Any],
    ) -> None:
        """Fire-and-forget warm of a ``(user, server)`` pool — e.g. right after
        OAuth consent — so the user's tool catalog populates immediately WITHOUT
        holding the consent redirect on a slow/unreachable MCP server.

        No-op for non-``oauth_user`` servers, before the mcp-loop is running, or
        with no token. Schedules the connect onto the mcp-loop and returns at
        once; the per-user tool listeners deliver the catalog to live sessions
        when the prime completes, and lazy dispatch remains the backstop.
        """
        if server_name not in self._oauth_user_server_names:
            return
        loop = self._loop
        if loop is None or not access_token:
            return
        cfg = _pool_cfg_from_row(server_row)
        key = (user_id, server_name)
        try:
            asyncio.run_coroutine_threadsafe(
                self._prime_user_server_logged(key, cfg, access_token, user_id, server_name),
                loop,
            )
        except RuntimeError:
            # mcp-loop is shutting down — skip; lazy dispatch is the backstop.
            log.debug("mcp pool prime skipped: loop closed user=%s server=%s", user_id, server_name)

    async def _prime_user_server_logged(
        self,
        key: tuple[str, str],
        cfg: dict[str, Any],
        access_token: str,
        user_id: str,
        server_name: str,
    ) -> None:
        """Best-effort body scheduled by :meth:`schedule_prime_user_server`.

        Runs on the mcp-loop and never lets an exception escape onto it: a prime
        failure must not change the user-observable consent outcome.
        """
        try:
            count = await self._prime_user_server(key, cfg, access_token)
            log.info("mcp pool primed user=%s server=%s tools=%d", user_id, server_name, count)
        except Exception:
            log.warning(
                "mcp pool prime failed user=%s server=%s",
                user_id,
                server_name,
                exc_info=True,
            )

    def prime_user_pools(self, user_id: str) -> None:
        """Fire-and-forget: warm THIS user's pool-backed servers (oauth_user + oauth_obo).

        Called at ChatSession start so a per-user server's tools are present
        automatically — no manual reconnect after a reboot/upgrade, and no
        chicken-and-egg (the model can't dispatch a tool it can't see). For
        oauth_obo this is the ONLY way tools reach the catalog: there is no
        consent flow, so nothing else would warm the pool and the documented
        "mint on first dispatch" could never fire (the model would never see the
        tool to dispatch it). Only touches servers the user already has a stored
        token / captured credential for; skips servers already connected.
        Non-blocking: schedules onto the mcp-loop and returns immediately.
        """
        if not user_id or self._loop is None:
            return
        if not self._oauth_user_server_names and not self._obo_server_names:
            return  # no pool-backed servers — nothing to prime (no set allocation)
        if self._app_state is None or self._storage is None:
            return
        # run_coroutine_threadsafe keeps the task referenced by the loop while
        # it runs, so no strong-ref bookkeeping is needed here.
        try:
            asyncio.run_coroutine_threadsafe(self._prime_user_pools(user_id), self._loop)
        except RuntimeError:
            # mcp-loop is shutting down — skip; lazy dispatch is the backstop.
            log.debug("mcp pool prime skipped: loop closed user=%s", user_id)

    async def _prime_user_pools(self, user_id: str) -> None:
        """Warm THIS user's pool-backed servers — oauth_user AND oauth_obo (runs on the mcp-loop).

        Best-effort and transient-safe: each token is resolved via the SAME
        guarded state machine the lazy-dispatch path uses — for oauth_user
        :func:`get_user_access_token_classified` (refresh grant), for oauth_obo
        :func:`get_obo_access_token_classified` (mint from the captured
        credential). That refreshes/mints an expired / near-expiry access token
        and persists it, but a TRANSIENT AS/network
        failure keeps the token (``kind=refresh_failed_transient``, no revoke)
        and is simply skipped here — only a genuinely PERMANENT rejection (the
        user must re-consent anyway) clears it. This closes the chicken-and-egg
        where an expired token made priming skip the server, its tools never
        entered the per-user catalog, and lazy dispatch — the only OTHER refresh
        trigger — could therefore never fire, leaving the pool permanently cold
        and stuck "connecting" with no token. Servers are primed concurrently
        under ``_PRIME_MAX_CONCURRENCY`` so one slow/unreachable upstream can't
        stall the rest.
        """
        token_store = getattr(self._app_state, "mcp_token_store", None)
        if token_store is None:
            return
        sem = asyncio.Semaphore(_PRIME_MAX_CONCURRENCY)

        async def _prime_one(server_name: str) -> None:
            key = (user_id, server_name)
            entry = self._user_pool_entries.get(key)
            if entry is not None and entry.session is not None:
                return  # already connected — nothing to do
            # Pre-lookup observation for the dead-grant drop below: at
            # this point the session is None; one connected during the
            # lookup's awaits must read as re-consent evidence.
            observed_session = self._observed_pool_session(user_id, server_name)
            if key in self._priming_keys:
                return  # a concurrent prime for this (user, server) is in flight
            # Claim synchronously before any await — the mcp-loop is single-
            # threaded, so check-then-add can't interleave with another coroutine.
            self._priming_keys.add(key)
            try:
                async with sem:
                    try:
                        # Branch on the in-memory obo registry (no SQL) to pick the
                        # lookup path. For oauth_obo we need the row up front (mint
                        # takes server_row + it feeds cfg); for oauth_user we defer
                        # the row fetch until AFTER a successful lookup so a user
                        # with no token pays no SELECT (the pre-obo behaviour).
                        # Resolve via the same guarded state machine lazy dispatch
                        # uses. For oauth_obo this MINTS from the captured credential
                        # (missing credential → kind="missing", skipped — the
                        # re-login rail handles it on real dispatch). For oauth_user,
                        # revoke_ambiguous_escalation=False: a genuinely-dead grant is
                        # still revoked so the catalog isn't left cold behind a
                        # phantom "consented" token, but a sustained-UNCLASSIFIABLE
                        # rejection is deferred to lazy dispatch. Only kind=="token"
                        # warms the pool.
                        server_row: dict[str, Any] | None = None
                        if server_name in self._obo_server_names:
                            server_row = await asyncio.to_thread(
                                self._storage.get_mcp_server_by_name, server_name
                            )
                            if not server_row:
                                return
                            lookup = await get_obo_access_token_classified(
                                app_state=self._app_state,
                                user_id=user_id,
                                server_name=server_name,
                                server_row=server_row,
                                # Same as the oauth_user priming call below: a
                                # sustained-UNCLASSIFIABLE IdP failure during a
                                # bulk session-start prime must NOT escalate-revoke
                                # (drop the cache row + arm cooldown) across every
                                # user — defer that to the user's real dispatch.
                                revoke_ambiguous_escalation=False,
                                # The credential-presence gate above already
                                # confirmed the captured credential exists for this
                                # user (one read for ALL their obo servers), so skip
                                # the per-server pre-lock existence re-read.
                                credential_present=True,
                            )
                        else:
                            lookup = await get_user_access_token_classified(
                                app_state=self._app_state,
                                user_id=user_id,
                                server_name=server_name,
                                revoke_ambiguous_escalation=False,
                            )
                        if lookup.kind != "token" or not lookup.token:
                            # Priming is also a convergence point (#836):
                            # a NEW session's prime discovering the grant
                            # is durably gone must drop the retained
                            # catalog other live sessions still serve —
                            # e.g. a disconnect made on another node.
                            self._schedule_dead_grant_drop(
                                lookup, key, observed_session=observed_session
                            )
                            return  # not consented / no credential / refresh failed — lazy paths handle it
                        if server_row is None:
                            server_row = await asyncio.to_thread(
                                self._storage.get_mcp_server_by_name, server_name
                            )
                            if not server_row:
                                return
                        cfg = _pool_cfg_from_row(server_row)
                        await self._prime_user_server(key, cfg, lookup.token)
                        log.info(
                            "mcp pool auto-primed at session start user=%s server=%s",
                            user_id,
                            server_name,
                        )
                    except Exception:
                        log.debug(
                            "mcp pool auto-prime failed user=%s server=%s",
                            user_id,
                            server_name,
                            exc_info=True,
                        )
            finally:
                self._priming_keys.discard(key)

        prime_names = self._pool_server_names
        if self._obo_server_names:
            # One raw existence SELECT (no decrypt) decides ALL obo servers for
            # this user: a user with no captured credential — a local-auth
            # account, or capture disabled when they last logged in — would
            # otherwise pay three SQL reads per obo server per session start
            # (server row + cache row + credential) just to learn
            # kind="missing". The oauth_user branch keeps its own deferred-row
            # optimisation. On any read hiccup, fail open and let the
            # per-server mint path classify it.
            # Pre-read observation for the dead-grant drops below: a
            # session connected DURING the credential read (a re-login
            # capture prime racing this one) must read as re-consent
            # evidence at drop time, not as the pre-observation session.
            observed_obo = {
                name: self._observed_pool_session(user_id, name) for name in self._obo_server_names
            }
            issuer = str(getattr(getattr(self._app_state, "oidc_config", None), "issuer", "") or "")
            has_credential = False
            if issuer:
                try:
                    has_credential = (
                        await asyncio.to_thread(
                            self._storage.get_oidc_user_credential, user_id, issuer
                        )
                        is not None
                    )
                except Exception:
                    has_credential = True
            if not has_credential:
                # The credential is GONE — fire the same dead-grant
                # convergence the per-server lookup would have
                # (kind="missing") for any retained obo entries, or
                # ghosts survive every new-session prime: this gate
                # skips exactly the lookup that would drop them (the
                # schedule-side guard skips entries with nothing to
                # converge).
                # Contract: the synthesized kind="missing" mirrors
                # get_obo_access_token_classified's durably-absent-
                # credential verdict (its missing-credential return
                # notes this back-reference); if that classification
                # ever splits — say a transient sub-case — update this
                # synthesis with it.
                for name in self._obo_server_names:
                    self._schedule_dead_grant_drop(
                        TokenLookupResult(kind="missing"),
                        (user_id, name),
                        observed_session=observed_obo.get(name),
                    )
                prime_names -= self._obo_server_names
        await asyncio.gather(*(_prime_one(s) for s in list(prime_names)))

    # -- background token-freshness sweep (oauth_user grants) -----------------

    async def _user_token_sweep_loop(self) -> None:
        """Long-running coroutine — keeps every consented ``oauth_user`` grant hot.

        Turnstone's OAuth token refresh is otherwise entirely LAZY: a token is
        refreshed only when a tool is dispatched or a session binds the acting
        user, and a dead refresh token (needs re-consent) is discovered only when
        a dispatch fails. That assumes a human is driving the session — it breaks
        for unattended / autonomous work, where a scheduled run or an
        alert-triggered workstream acts on behalf of a user who is NOT present and
        must find the token already fresh and the grant already known-good.

        This loop closes that gap WITHOUT keeping connections warm and WITHOUT
        mutating consent state on a timer (:meth:`_reconcile_one_user_token` is
        observe-only). Each tick it walks every consented ``(user, server)`` pair
        and runs the canonical refresh path
        (:func:`get_user_access_token_classified`), which (a) refreshes an access
        token within the 60s expiry skew AND — via the keepalive gate
        (:meth:`_keepalive_refresh_due`) — force-refreshes a grant whose refresh
        token has sat un-exercised past the keepalive window, so a provider that
        ages out *idle* refresh tokens can't expire one between the user's real
        sessions even when the access-token TTL is long; and (b) surfaces a dead
        grant as a proactive re-consent badge, ahead of the work that needs it
        instead of at dispatch time. Connections stay lazy: one cheap reconnect
        on first dispatch is fine; a failed or stale-token dispatch is not.

        STRICTLY ``oauth_user``-scoped. :meth:`_sweep_user_token_freshness`
        early-returns when no ``oauth_user`` server is configured, so a
        static-only / no-auth deployment does no AS round-trips, no MCP-server
        calls, and no token-table scans — just a set-emptiness check per tick.

        The FIRST sweep runs after only a short startup grace (not a full
        cadence) so a restart surfaces a grant that died during downtime within
        seconds; every subsequent sweep waits the full cadence.
        """
        delay = min(self._USER_TOKEN_SWEEP_STARTUP_GRACE_S, self._user_token_sweep_s)
        while True:
            try:
                await asyncio.sleep(delay)
                await self._sweep_user_token_freshness()
            except asyncio.CancelledError:
                return
            except (Exception, BaseExceptionGroup):
                # BaseExceptionGroup: same rationale as ``_static_health_loop``
                # — a transport-layer group must not kill the loop.
                log.warning("MCP token-freshness sweep iteration failed", exc_info=True)
            delay = self._user_token_sweep_s  # subsequent sweeps wait the full cadence

    async def _sweep_user_token_freshness(self) -> None:
        """One pass: refresh + classify every consented ``oauth_user`` grant.

        MUST run on the mcp-loop. The gate below IS the no-auth safety property:
        with no ``oauth_user`` server, or before storage / app_state are wired,
        this returns before touching the DB or any AS — a static / no-auth server
        (whose rows :func:`_db_servers_to_config` strips out, and which never
        writes a user-token row) is structurally invisible to this pass. Per-pair
        failures are isolated so one bad grant can't starve the rest.
        """
        if self._storage is None or self._app_state is None or not self._oauth_user_server_names:
            return
        try:
            targets = await asyncio.to_thread(self._storage.list_mcp_user_token_reconcile_targets)
        except Exception:
            log.debug("MCP token sweep: failed to enumerate consented grants", exc_info=True)
            return
        oauth_servers = self._oauth_user_server_names
        seen: set[tuple[str, str]] = set()
        for user_id, server_name, last_exercised in targets:
            if server_name not in oauth_servers:
                # A token row lingering for a server that is no longer
                # auth_type=oauth_user (renamed / demoted) — not ours to keep hot.
                continue
            key = (user_id, server_name)
            seen.add(key)
            try:
                # Force a refresh when the refresh token has sat un-exercised past
                # the keepalive window, so a provider that ages out idle refresh
                # tokens can't expire one between the user's real sessions.
                await self._reconcile_one_user_token(
                    key, force_refresh=self._keepalive_refresh_due(last_exercised)
                )
            except Exception:
                log.debug(
                    "MCP token sweep: reconcile failed user=%s server=%s",
                    user_id,
                    server_name,
                    exc_info=True,
                )
        # Prune the once-only surfacing set to pairs still consented this pass, so
        # a re-consented or revoked-then-recreated grant re-arms (and the set can
        # never grow unbounded across many transient dead grants).
        self._token_sweep_warned &= seen

    async def _reconcile_one_user_token(
        self, key: tuple[str, str], *, force_refresh: bool = False
    ) -> None:
        """Refresh-on-expiry + dead-grant classification for one ``(user, server)``.

        OBSERVE-ONLY: runs the canonical lookup with ``revoke_on_failure=False``
        so a background timer NEVER deletes a token or moves a foreground user's
        revoke threshold — a dead grant is only *surfaced*, and the authoritative
        revoke stays on the lazy-dispatch path where a real user action justifies
        it. NEVER connects to the MCP server: only the token is kept hot. Because
        the row survives, the pair is re-checked every tick, so a spurious
        server-wide ``invalid_grant`` (an AS maintenance window) self-heals — the
        badge is dropped on the tick the grant works again.

        ``force_refresh`` (set by the keepalive gate) exercises the refresh token
        even while the access token is still fresh, so a provider that ages out
        idle refresh tokens can't expire one between the user's real sessions.

        Surfacing is once-per-transition (tracked in ``_token_sweep_warned``):
        ``refresh_failed`` (dead grant) raises the dashboard pending-consent
        badge — and the pin is set ONLY after a successful persist, so a failed
        badge write retries next tick rather than being lost forever;
        ``decrypt_failure`` (operator key issue) is logged but NOT badged (the
        same split :meth:`_record_pending_consent_best_effort` applies).
        """
        user_id, server_name = key
        lookup = await get_user_access_token_classified(
            app_state=self._app_state,
            user_id=user_id,
            server_name=server_name,
            revoke_ambiguous_escalation=False,
            revoke_on_failure=False,
            force_refresh=force_refresh,
        )
        if lookup.kind == "refresh_failed":
            # Dead grant: only a human re-consent in a browser fixes it. Badge it
            # proactively so the user sees the affordance ahead of autonomous use.
            if key not in self._token_sweep_warned:
                log.warning(
                    "MCP token sweep: grant for user=%s server=%s needs re-consent "
                    "(dead refresh token) — surfaced proactively before autonomous use",
                    user_id,
                    server_name,
                )
                # Pin only on a durable badge — a failed write is retried next
                # tick (the observe-only row survives to be re-enumerated).
                if await self._persist_pending_consent_best_effort(user_id, server_name):
                    self._token_sweep_warned.add(key)
        elif lookup.kind == "decrypt_failure":
            # Undecryptable stored token — an operator key issue, not
            # user-fixable and outside the consent dashboard's scope. Surface it
            # once; do not badge.
            if key not in self._token_sweep_warned:
                self._token_sweep_warned.add(key)
                log.warning(
                    "MCP token sweep: stored token for user=%s server=%s is undecryptable "
                    "(operator key issue) — cannot keep this grant hot",
                    user_id,
                    server_name,
                )
        elif lookup.kind in ("token", "missing") and key in self._token_sweep_warned:
            # Usable again (fresh / freshly-refreshed) or gone (de-consented). On
            # a transition out of a surfaced state, re-arm and drop any stale
            # badge — the self-heal for a spurious invalid_grant that recovered
            # (no-op if none was raised, e.g. a decrypt_error).
            self._token_sweep_warned.discard(key)
            await self._clear_pending_consent_best_effort(user_id, server_name)
        # refresh_failed_transient: AS / network blip — the token is retained and
        # the next tick retries. Not human-actionable, so nothing is surfaced and
        # the prior warned state (if any) is left intact.

    def _keepalive_refresh_due(self, last_exercised_iso: str | None) -> bool:
        """True when a grant's refresh token has sat un-exercised past the
        keepalive window and should be force-refreshed to stay alive.

        Disabled (``_user_token_refresh_keepalive_s <= 0``) → never. An unknown /
        unparseable timestamp → force once (safe: exercising a live token is
        harmless; missing the keepalive is the failure we're preventing). The
        stored timestamp is naive-UTC ISO (``%Y-%m-%dT%H:%M:%S``).
        """
        if self._user_token_refresh_keepalive_s <= 0:
            return False
        if not last_exercised_iso:
            return True
        try:
            last = datetime.fromisoformat(last_exercised_iso)
        except (TypeError, ValueError):
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last).total_seconds() >= self._user_token_refresh_keepalive_s

    def _write_pending_consent(
        self, user_id: str, server_name: str, *, error_code: str, scopes_required: str | None
    ) -> None:
        """Single source of the deferred-consent upsert call shape (blocking).

        Both best-effort wrappers — the sync dispatch-path
        :meth:`_record_pending_consent_best_effort` and the loop-path
        :meth:`_persist_pending_consent_best_effort` — route through here so the
        5-kwarg ``upsert_mcp_pending_consent`` call and the timestamp format live
        in ONE place.  Raises on storage error — callers wrap best-effort.
        """
        if self._storage is None:
            return
        self._storage.upsert_mcp_pending_consent(
            user_id=user_id,
            server_name=server_name,
            error_code=error_code,
            scopes_required=scopes_required,
            now_iso=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        )
        # A fresh pending row exists again → un-mark the pair so the next
        # successful dispatch on this node clears it without waiting out the
        # TTL (other nodes converge via their own TTL expiry).
        self._pending_consent_cleared.pop((user_id, server_name), None)

    async def _persist_pending_consent_best_effort(self, user_id: str, server_name: str) -> bool:
        """Raise the dashboard pending-consent badge for a proactively-detected
        dead grant, off the mcp-loop. Returns True iff the badge was persisted.

        The sweep runs ON the loop, so the blocking storage write goes through
        :func:`asyncio.to_thread`. Maps to the ``mcp_consent_required`` code the
        dashboard already renders. Best-effort: a storage failure is logged and
        returns False (never raised onto the loop) so the caller retries on the
        next tick rather than pinning a badge that was never written.
        """
        if self._storage is None:
            return False
        try:
            await asyncio.to_thread(
                self._write_pending_consent,
                user_id,
                server_name,
                error_code="mcp_consent_required",
                scopes_required=None,
            )
            return True
        except Exception:
            log.warning(
                "MCP token sweep: pending-consent badge persist failed user=%s server=%s",
                user_id,
                server_name,
                exc_info=True,
            )
            return False

    async def _clear_pending_consent_best_effort(self, user_id: str, server_name: str) -> None:
        """Drop a stale pending-consent badge when a previously-surfaced grant is
        usable again (real re-consent, or a spurious ``invalid_grant`` that has
        healed), off the mcp-loop. No-op if no badge was raised. Best-effort: a
        storage failure is logged, never raised onto the loop.
        """
        if self._storage is None:
            return
        try:
            await asyncio.to_thread(self._storage.delete_mcp_pending_consent, user_id, server_name)
            # Route through the shared helper (not an inline prune+stamp) so the
            # two DB-confirmed clear sites can't drift — see
            # _mark_pending_consent_cleared, which names this method as a caller.
            self._mark_pending_consent_cleared((user_id, server_name), time.monotonic())
        except Exception:
            log.debug(
                "MCP token sweep: pending-consent clear failed user=%s server=%s",
                user_id,
                server_name,
                exc_info=True,
            )

    # -- pool eviction --------------------------------------------------------

    async def _user_pool_eviction_loop(self) -> None:
        """Long-running coroutine — periodically evicts idle pool entries.

        Note: the loop wakes on a fixed tick rather than a condition
        variable. Event-driven evictions would be more efficient on
        large idle pools but add complexity (tracking per-entry
        deadlines + a wakeup ``asyncio.Event``). Warm entries are
        bounded by the LRU cap (default 200); cooled catalog-only
        entries are bounded by live-session users × registered pool
        servers, and each tick's work over them is a per-entry set
        lookup against a once-per-tick listener snapshot — negligible
        either way.
        """
        while True:
            try:
                await asyncio.sleep(self._POOL_EVICTION_TICK_S)
                await self._evict_idle_pool_entries()
            except asyncio.CancelledError:
                return
            except (Exception, BaseExceptionGroup):
                # BaseExceptionGroup: same rationale as ``_static_health_loop``
                # — a transport-layer group must not kill the loop.
                log.warning("MCP pool eviction iteration failed", exc_info=True)

    def has_live_session_listener(self, user_id: str) -> bool:
        """True when *user_id* has a registered user-scoped tool listener.

        A live listener means a live ChatSession whose merged tool list
        is derived from this user's pool catalog. The ONE liveness
        predicate: the eviction passes use it to decide cool-vs-drop
        (#836), and outside callers (the OIDC capture-site prime) use
        it to fan out work only for users who actually have an open
        session to heal — priming on every routine SSO re-login would
        warm transports and mint tokens for users with nothing open, at
        deployment scale. Admin (``None``) listeners don't count: they
        track catalog state for operator tooling, not a user's
        model-visible tool list.
        """
        with self._listeners_lock:
            return any(uid == user_id for uid, _cb in self._listeners)

    def _live_listener_uids(self) -> set[str]:
        """Snapshot the user ids with a live tool listener (one lock take).

        The TTL pass checks retention for every idle entry every tick;
        a per-entry ``has_live_session_listener`` scan would be
        O(entries × listeners) under ``_listeners_lock`` on the
        mcp-loop. Snapshot staleness is bounded by one tick and benign:
        a listener added after the snapshot re-primes at session start
        anyway, and one removed after it is caught next tick.
        """
        with self._listeners_lock:
            return {uid for uid, _cb in self._listeners if uid}

    @staticmethod
    def _entry_has_catalog(entry: PoolEntryState) -> bool:
        """True when the entry carries a discovered catalog worth retaining.

        Never-connected stubs (``_ensure_pool_entry`` allocated, connect
        failed before discovery) and revoke-cleared entries
        (:meth:`_evict_session_drop_catalog`) carry none — retaining
        those serves nobody, so the eviction passes full-drop them even
        when the user has a live session.
        """
        return entry.tools is not None or entry.resources is not None or entry.prompts is not None

    @staticmethod
    def _entry_is_warm(entry: PoolEntryState) -> bool:
        """True when the entry holds connection resources.

        Warm = an open session OR a live owner task (an owner parked
        after ``_evict_session`` still holds the transport until the
        close protocol runs). Cooled catalog-only entries hold neither.
        This is THE definition the LRU cap bounds; every warm check in
        the eviction passes must go through it.
        """
        return entry.session is not None or entry.owner_task is not None

    def _warm_pool_count(self) -> int:
        """Live count of entries holding connection resources (:meth:`_entry_is_warm`)."""
        return sum(1 for e in self._user_pool_entries.values() if self._entry_is_warm(e))

    def _retain_cooled(
        self,
        key: tuple[str, str],
        entry: PoolEntryState,
        live_uids: set[str] | None = None,
    ) -> bool:
        """Retention policy: keep this entry cooled for a live session?

        True iff ALL of:
        - the entry carries a discovered catalog (stubs and
          revoke-cleared entries retain nothing);
        - the server still exists as a pool server in the in-memory
          registries (:meth:`_is_pool_server`, rebuilt wholesale by
          every reconcile). Without this, an admin delete / disable /
          rename / auth-flip left an IMMORTAL cooled entry serving
          ghost tools to live sessions — pre-#836-fix those aged out
          with the idle TTL; with the registry check they full-drop
          within one eviction tick, faster than before;
        - the user has a live session (a registered user-scoped tool
          listener). ``live_uids`` is the per-tick snapshot; ``None``
          consults the registry directly (authoritative, single key).

        Single copy of the policy — the TTL pass's already-cooled skip
        and :meth:`_close_pool_entry_if_idle`'s cool-vs-drop decision
        must never diverge.

        Accepted residual: the two pool-name registries are replaced by
        ``reconcile_sync`` on a server thread as two adjacent stores, so
        a same-tick read here can theoretically observe a flip
        mid-swap (in neither set) and full-drop a cooled entry. The
        window is one bytecode boundary, and the same reconcile's
        flip re-prime restores the catalog seconds later — an atomic
        single-source registry is not worth the churn for that.
        """
        user_id, server_name = key
        if not self._entry_has_catalog(entry):
            return False
        if not self._is_pool_server(server_name):
            return False
        if live_uids is not None:
            return user_id in live_uids
        return self.has_live_session_listener(user_id)

    def _rebuild_and_notify_user_catalogs(self, user_id: str) -> None:
        """Rebuild all three per-user catalog maps, then fan out to all
        three listener classes (user-keyed + admin ``None``).

        MUST run on the mcp-loop. Rebuild-before-notify is the
        invariant (listeners re-read the maps), and tools / resources /
        prompts move together — the Phase 7 round-2 "bug-pair" was
        exactly a tools-only cleanup missing its resource/prompt half.
        Shared by connect wiring, the full-drop eviction path, and the
        revocation drop.
        """
        self._rebuild_user_tool_map(user_id)
        self._rebuild_user_resource_map(user_id)
        self._rebuild_user_prompt_map(user_id)
        self._notify_user_tool_listeners(user_id)
        self._notify_user_resource_listeners(user_id)
        self._notify_user_prompt_listeners(user_id)

    async def _evict_idle_pool_entries(self) -> None:
        """Evict pool entries past the idle TTL or above the LRU cap.

        Skips any key whose ``open_lock`` is currently held or whose
        ``in_flight`` counter is non-zero — eviction never blocks on a
        contested lock or an active dispatch; the next tick retries.

        Both passes close TRANSPORTS. Whether the ENTRY (and with it the
        user's catalog contribution) survives is decided per user in
        :meth:`_close_pool_entry_if_idle`: a user with a live session
        keeps a cooled catalog-only entry so their model-visible tool
        list never silently shrinks (#836); a user without one gets the
        full drop. The LRU cap therefore bounds WARM entries — the
        connection resources (transport, httpx client, owner task) are
        what the cap exists to limit. Cooled entries hold none of
        those; they are bounded by live-session users × pool servers
        and reaped by the TTL pass within a tick of their user's last
        listener going away.
        """
        if not self._user_pool_entries:
            return
        now = time.monotonic()
        ttl = self._user_pool_idle_ttl_s
        live_uids = self._live_listener_uids()

        # First pass: TTL-based eviction. Run closes in parallel so a tick
        # that needs to evict many entries doesn't block on serial teardowns.
        ttl_targets: list[tuple[str, str]] = []
        for key, entry in list(self._user_pool_entries.items()):
            last = self._user_pool_last_used.get(key, entry.last_used)
            if (now - last) < ttl:
                continue
            if not self._entry_is_warm(entry) and self._retain_cooled(key, entry, live_uids):
                # Already cooled — nothing to close. Retained for the
                # live session's tool list; once the user's last
                # listener goes away OR the server leaves the pool
                # registries, this stops matching and the entry takes
                # the full-drop path below.
                continue
            ttl_targets.append(key)
        if ttl_targets:
            await asyncio.gather(
                *(self._close_pool_entry_if_idle(k) for k in ttl_targets),
                return_exceptions=True,
            )

        # Second pass: LRU cap over warm entries. Iterate the canonical
        # entry map (not the last_used view) so brand-new entries that
        # were created via ``_ensure_pool_entry`` but haven't dispatched
        # yet are still eviction-eligible.
        warm = [
            (key, entry)
            for key, entry in self._user_pool_entries.items()
            if self._entry_is_warm(entry)
        ]
        if len(warm) <= self._user_pool_lru_max:
            return
        ordered = sorted(
            warm,
            key=lambda kv: self._user_pool_last_used.get(kv[0], kv[1].last_used),
        )
        # Re-check the LIVE warm count each iteration — concurrent
        # tasks change the warm set during this pass's awaits
        # (revocation evictions, owner deaths, new connects), and a
        # snapshot delta would keep closing healthy transports below
        # the cap or stop above it. O(entries) per close is fine at
        # cap scale; cooled entries are excluded from the count.
        for key, _entry in ordered:
            if self._warm_pool_count() <= self._user_pool_lru_max:
                break
            await self._close_pool_entry_if_idle(key)

    async def _close_pool_entry_if_idle(self, key: tuple[str, str]) -> None:
        """Close ``key``'s transport iff its open_lock is uncontested AND in_flight==0.

        Best-effort: a contested lock or an active dispatch causes the
        function to return without mutation; the next eviction tick
        retries.

        What happens to the ENTRY is :meth:`_retain_cooled` — ONE
        policy shared with the TTL pass's already-cooled skip:

        - retained → the entry is COOLED: transport torn down, catalog
          kept, no rebuild, no listener fan-out. The user's merged tool
          list is untouched and the next dispatch or prime reconnects —
          the evict-session-keep-entry shape of
          ``_on_pool_owner_death``. Dropping the catalog here instead
          silently removed the server's tools from live sessions with
          no re-prime path (#836).
        - otherwise → full drop: entry popped, per-user catalogs
          rebuilt, listeners notified. Covers departed users (no live
          listener), catalog-less stubs, and servers that left the pool
          registries (deleted / disabled / renamed / auth-flipped) —
          whose ghost tools must leave live sessions within a tick.
        """
        entry = self._user_pool_entries.get(key)
        if entry is None:
            return
        # In-flight dispatchers may have released ``open_lock`` after the
        # connect/reuse window (see ``_dispatch_pool_with_entry``); the
        # ``in_flight`` counter is the source of truth for whether the
        # session is currently being used.
        if entry.in_flight > 0:
            return
        lock = entry.open_lock
        if lock.locked():
            return
        try:
            await asyncio.wait_for(
                lock.acquire(), timeout=self._POOL_EVICTION_LOCK_ACQUIRE_TIMEOUT_S
            )
        except (TimeoutError, asyncio.CancelledError):
            return
        evicted = False
        try:
            entry = self._user_pool_entries.get(key)
            if entry is None:
                return
            # Re-check in_flight under the lock — a dispatcher may have
            # bumped the counter between our pre-acquire skip check and
            # the lock acquisition.
            if entry.in_flight > 0:
                return
            await self._teardown_pool_entry(key)
            user_id, _server_name = key
            if self._retain_cooled(key, entry):
                # Cooled: entry + catalog stay, so the live session's
                # tool list and ``is_mcp_tool`` are untouched. Nothing
                # changed catalog-wise → no rebuild, no fan-out. The
                # entry's ``open_lock`` must survive with it (an
                # in-flight dispatcher's next acquire needs the same
                # lock object), so skip the ``evicted`` cleanup too.
                return
            had_catalog = self._entry_has_catalog(entry)
            self._user_pool_entries.pop(key, None)
            self._user_pool_last_used.pop(key, None)
            if had_catalog:
                # Dropping the entry without rebuilding the per-user
                # catalogs would leave ``is_mcp_tool`` / per-user
                # resource & prompt maps returning stale entries whose
                # backing pool is gone for good (departed user, or a
                # server no longer in the pool registries). A
                # catalog-less stub contributed nothing — skip the
                # zero-delta fan-out.
                self._rebuild_and_notify_user_catalogs(user_id)
            evicted = True
        finally:
            lock.release()
        # Drop the now-orphaned lock so the dict doesn't grow without
        # bound across the process lifetime — but ONLY on the success
        # path. The early-return branches (entry already removed by a
        # racing path, or in_flight > 0) leave the lock in place: an
        # in-flight dispatcher needs the same lock object on its next
        # acquire, and a key whose entry races a concurrent eviction
        # will be re-allocated by ``_ensure_pool_entry`` next time.
        if evicted:
            self._user_pool_locks.pop(key, None)

    # -- failure classification (pool dispatch) ------------------------------

    def _classify_failure(
        self,
        exc: BaseException,
        *,
        capture: _AuthCapture | None = None,
    ) -> Literal["transport", "auth_401", "auth_403", "protocol", "other"]:
        """Classify a dispatch-time exception for circuit-breaker gating.

        Only ``transport`` failures trip the per-server breaker. Auth
        failures (401/403) are pool-entry-only — they never affect the
        breaker. Protocol errors (``McpError``) come from a healthy
        connection that rejected the request.

        Auth detection prefers ``capture.status`` (response-hook
        introspection — the SDK swallows :class:`httpx.HTTPStatusError`
        in its ``post_writer`` so the carrier is the only signal that
        reaches us in production). The ``HTTPStatusError`` fallback is
        defense-in-depth for the non-SDK refresh path
        (:func:`turnstone.core.mcp_oauth._refresh_and_persist`) where
        ``httpx`` errors propagate directly.
        """
        if capture is not None and capture.status == 401:
            return "auth_401"
        if capture is not None and capture.status == 403:
            return "auth_403"
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 401:
                return "auth_401"
            if status == 403:
                return "auth_403"
        # A closed/broken transport must be classified BEFORE the McpError
        # branch: the SDK surfaces a dead connection as McpError(CONNECTION_CLOSED),
        # which would otherwise be mistaken for a healthy protocol rejection and
        # leave the dead pool entry in place forever (no rebuild).
        if _is_dead_transport(exc):
            return "transport"
        if isinstance(exc, McpError):
            return "protocol"
        if isinstance(exc, TimeoutError):
            return "transport"
        return "other"

    # -- tool refresh --------------------------------------------------------

    def _rebuild_tools(self) -> None:
        """Rebuild merged ``_tools`` and ``_tool_map`` from per-server state.

        Uses copy-on-write: builds new objects, then assigns atomically.
        Concurrent readers see either the old or new snapshot — both valid.

        Every entry sourced from ``_static_servers`` is, by construction,
        NOT ``auth_type='oauth_user'`` — :func:`_db_servers_to_config`
        strips oauth_user rows on the way in. Pool catalogs do not
        contribute to ``_tool_map`` today; pool tools become reachable
        via ``_tool_map`` only once per-user catalog scoping lands.
        """
        new_tools: list[dict[str, Any]] = []
        new_map: dict[str, tuple[str, str]] = {}
        for srv_name, srv_state in self._static_servers.items():
            for tool in srv_state.tools:
                prefixed: str = tool["function"]["name"]
                new_tools.append(tool)
                # Extract original name from the mcp__server__original pattern
                original = prefixed.split("__", 2)[2] if prefixed.count("__") >= 2 else prefixed
                new_map[prefixed] = (srv_name, original)
        self._tools = new_tools
        self._tool_map = new_map
        self._notify_listeners()

    def _rebuild_user_tool_map(self, user_id: str) -> None:
        """Rebuild the per-user prefixed-name index from pool entries.

        Mirrors :meth:`_rebuild_tools` for one user's pool entries:
        scan ``_user_pool_entries`` for keys whose first element matches
        ``user_id``, materialize a fresh ``prefixed_name → (server, original)``
        dict and a parallel tool-list, assign each to its dict in
        ``_user_tool_map`` and ``_user_tools`` respectively. Each
        per-key write is individually atomic under the GIL; the two
        writes happen back-to-back on the mcp-loop with no awaits
        between them, so a sync-thread reader cannot interleave at the
        Python statement level — but the cross-dict write is not a
        single atomic operation. In practice the window is sub-microsecond
        and the listener fan-out (which fires AFTER both writes
        complete) is the trigger for any session-side rebuild that
        might re-read both dicts.

        ``_tool_map`` (the static index) is NEVER mutated here, so
        invariant 1 (static path byte-identical) is preserved.

        Empty rebuilds drop the user_id key from BOTH dicts so an idle
        user with no pool entries doesn't retain permanent empty-list
        sentinels.

        MUST run on the mcp-loop. The pool dict scan here cannot race
        with sync-thread reads because sync threads never touch
        ``_user_pool_entries``; they read ``_user_tools`` instead.
        """
        new_map: dict[str, tuple[str, str]] = {}
        new_tools: list[dict[str, Any]] = []
        for (uid, server_name), entry in self._user_pool_entries.items():
            if uid != user_id or entry.tools is None:
                continue
            for tool in entry.tools:
                prefixed: str = tool["function"]["name"]
                # Extract original name from the mcp__server__original pattern.
                original = prefixed.split("__", 2)[2] if prefixed.count("__") >= 2 else prefixed
                new_map[prefixed] = (server_name, original)
                new_tools.append(tool)
        if new_map:
            self._user_tool_map[user_id] = new_map
            self._user_tools[user_id] = new_tools
        else:
            self._user_tool_map.pop(user_id, None)
            self._user_tools.pop(user_id, None)

    def _rebuild_user_resource_map(self, user_id: str) -> None:
        """Rebuild the per-user resource index from pool entries.

        Mirrors :meth:`_rebuild_user_tool_map` for resources (RFC §3.2):
        scan ``_user_pool_entries`` for keys whose first element matches
        ``user_id``, materialize a fresh ``uri → (server, uri)`` dict, a
        parallel resource list, AND a parallel template-prefix dict,
        and assign each atomically.

        URI collisions across pool servers within a single user's pool
        follow the same "later wins, log warning" policy as the static
        :meth:`_rebuild_resources` (mcp_client.py:1689-1744). Empty
        rebuilds drop the user_id key from all three dicts so idle
        users don't retain empty-list sentinels.

        ``_resource_map`` / ``_template_prefixes`` (the static indices)
        are NEVER mutated here, preserving invariant 1.

        MUST run on the mcp-loop. The two writes (``_user_resource_map``,
        ``_user_resources``, ``_user_template_prefixes``) happen
        back-to-back with no awaits between them so a sync-thread reader
        cannot observe a torn state.
        """
        new_map: dict[str, tuple[str, str]] = {}
        new_resources: list[dict[str, Any]] = []
        new_prefixes: dict[str, tuple[str, str]] = {}
        for (uid, server_name), entry in self._user_pool_entries.items():
            if uid != user_id or entry.resources is None:
                continue
            for res in entry.resources:
                if res.get("template"):
                    tmpl_uri: str = res["uri"]
                    brace = tmpl_uri.find("{")
                    prefix = tmpl_uri[:brace] if brace >= 0 else tmpl_uri
                    if prefix:
                        if prefix in new_prefixes:
                            existing_srv, existing_tmpl = new_prefixes[prefix]
                            if len(tmpl_uri) > len(existing_tmpl):
                                log.warning(
                                    "Pool template prefix collision: '%s' from '%s' overrides "
                                    "'%s' (keeping more specific template) user=%s",
                                    prefix,
                                    server_name,
                                    existing_srv,
                                    user_id,
                                )
                                new_prefixes[prefix] = (server_name, tmpl_uri)
                            else:
                                log.warning(
                                    "Pool template prefix collision: '%s' from '%s' ignored in "
                                    "favor of '%s' (keeping more specific template) user=%s",
                                    prefix,
                                    server_name,
                                    existing_srv,
                                    user_id,
                                )
                        else:
                            new_prefixes[prefix] = (server_name, tmpl_uri)
                    new_resources.append(res)
                    continue
                uri: str = res["uri"]
                if uri in new_map:
                    log.warning(
                        "Pool resource URI collision: '%s' from '%s' overrides '%s' user=%s",
                        uri,
                        server_name,
                        new_map[uri][0],
                        user_id,
                    )
                new_map[uri] = (server_name, uri)
                new_resources.append(res)
        if new_map or new_resources or new_prefixes:
            self._user_resource_map[user_id] = new_map
            self._user_resources[user_id] = new_resources
            self._user_template_prefixes[user_id] = new_prefixes
        else:
            self._user_resource_map.pop(user_id, None)
            self._user_resources.pop(user_id, None)
            self._user_template_prefixes.pop(user_id, None)

    def _rebuild_user_prompt_map(self, user_id: str) -> None:
        """Rebuild the per-user prompt index from pool entries.

        Mirrors :meth:`_rebuild_user_tool_map` for prompts (RFC §3.3):
        scan ``_user_pool_entries`` for keys whose first element matches
        ``user_id``, materialize a fresh
        ``prefixed_name → (server, original_name)`` dict and a parallel
        prompt list, and assign each atomically.

        ``_prompt_map`` (the static index) is NEVER mutated here,
        preserving invariant 1.

        Empty rebuilds drop the user_id key from BOTH dicts.

        MUST run on the mcp-loop.
        """
        new_map: dict[str, tuple[str, str]] = {}
        new_prompts: list[dict[str, Any]] = []
        for (uid, _server_name), entry in self._user_pool_entries.items():
            if uid != user_id or entry.prompts is None:
                continue
            for prompt in entry.prompts:
                prefixed: str = prompt["name"]
                new_map[prefixed] = (prompt["server"], prompt["original_name"])
                new_prompts.append(prompt)
        if new_map:
            self._user_prompt_map[user_id] = new_map
            self._user_prompts[user_id] = new_prompts
        else:
            self._user_prompt_map.pop(user_id, None)
            self._user_prompts.pop(user_id, None)

    async def _refresh_server_tools(self, name: str) -> tuple[list[str], list[str]]:
        """Re-fetch tools for one server.  Returns ``(added, removed)`` names."""
        state = self._static_servers.get(name)
        if state is None or state.session is None:
            raise RuntimeError(f"MCP server '{name}' is not connected")
        # Capture session locally — a concurrent transport-error eviction in
        # call_tool_sync can clear state.session; reads after an await would
        # raise AttributeError without this snapshot.
        session = state.session

        old_names = {t["function"]["name"] for t in state.tools}

        # ``asyncio.timeout`` mandatory — a wedged server must not hang a
        # spawned refresh (and the connect lock it holds) forever; the pool
        # sibling (:meth:`_refresh_pool_server_tools`) already complies.
        async with asyncio.timeout(self._CONNECT_TIMEOUT):
            result = await session.list_tools()
        if self._static_servers.get(name) is not state:
            # The state entry was replaced (remove + re-add) while
            # list_tools was in flight — this result belongs to the old
            # transport; publishing it would clobber the new discovery.
            return [], []
        capped = _cap_server_tools(name, result.tools)
        server_tools = [_mcp_to_openai(name, tool) for tool in capped]
        new_names = {t["function"]["name"] for t in server_tools}

        state.tools = server_tools
        self._rebuild_tools()

        added = sorted(new_names - old_names)
        removed = sorted(old_names - new_names)
        if added or removed:
            log.info(
                "Refreshed MCP server '%s': +%d/-%d tool(s)",
                name,
                len(added),
                len(removed),
            )
        return added, removed

    async def _refresh_pool_server_tools(self, key: tuple[str, str]) -> tuple[list[str], list[str]]:
        """Re-fetch tools for one pool entry.  Returns ``(added, removed)`` names.

        Mirror of :meth:`_refresh_server_tools` for the pool path:
        targets a single ``(user_id, server_name)`` entry, mutates ONLY
        ``entry.tools`` + the per-user index, fires the user-scoped
        listener fan-out. Static-path catalogs are NEVER touched, so
        invariant 1 (static path byte-identical) is preserved.

        MUST run on the mcp-loop, with the entry's ``open_lock`` HELD
        (:meth:`_run_notification_refresh` acquires it): an unlocked
        refresh races a connect's discovery wiring and sibling
        refreshes, either of which can publish an older catalog over
        this one — see the runner's ordering rationale.
        """
        entry = self._user_pool_entries.get(key)
        if entry is None or entry.session is None:
            raise RuntimeError(f"Pool entry {key!r} is not connected")
        # Snapshot session locally — a concurrent transport-error
        # eviction can clear ``entry.session`` after our reads, so a
        # post-await ``entry.session.<...>`` call would raise
        # ``AttributeError``. Mirrors the static path's pattern.
        session = entry.session
        user_id, server_name = key
        old_names = {t["function"]["name"] for t in (entry.tools or [])}
        # ``asyncio.timeout`` mandatory — a wedged server must not hang
        # the spawned refresh task (and the ``open_lock`` it holds)
        # forever; the resource/prompt siblings already comply.
        async with asyncio.timeout(self._CONNECT_TIMEOUT):
            result = await session.list_tools()
        if self._user_pool_entries.get(key) is not entry:
            # The entry was replaced (full drop + re-create) while
            # list_tools was in flight — this result belongs to the
            # old entry; publishing it would clobber the new one.
            return [], []
        capped = _cap_server_tools(server_name, result.tools)
        server_tools = [_mcp_to_openai(server_name, tool) for tool in capped]
        new_names = {t["function"]["name"] for t in server_tools}
        entry.tools = server_tools
        self._rebuild_user_tool_map(user_id)
        # Fire user-keyed AND admin (None) listeners. Other users'
        # listeners do NOT see this change — the pool catalog is private.
        self._notify_user_tool_listeners(user_id)
        added = sorted(new_names - old_names)
        removed = sorted(old_names - new_names)
        if added or removed:
            log.info(
                "Refreshed pool MCP server user=%s server=%s: +%d/-%d tool(s)",
                user_id,
                server_name,
                len(added),
                len(removed),
            )
        return added, removed

    async def _refresh_pool_server_resources(
        self, key: tuple[str, str]
    ) -> tuple[list[str], list[str]]:
        """Re-fetch resources for one pool entry. Returns ``(added, removed)`` URIs.

        Mirror of :meth:`_refresh_pool_server_tools` for the resource
        path (RFC §3.2). Skips when ``supports_resources`` is False so
        a server that no longer advertises resources doesn't trigger
        a list call. ``asyncio.timeout`` is mandatory — a wedged server
        must not hang the refresh (and the lock it holds) forever.

        MUST run on the mcp-loop, with the entry's ``open_lock`` HELD
        (:meth:`_run_notification_refresh` acquires it): an unlocked
        refresh races a connect's discovery wiring and sibling
        refreshes, either of which can publish an older catalog over
        this one — see the runner's ordering rationale.
        """
        entry = self._user_pool_entries.get(key)
        if entry is None or entry.session is None or not entry.supports_resources:
            return [], []
        session = entry.session
        user_id, server_name = key
        old_uris = {r["uri"] for r in (entry.resources or []) if not r.get("template")}

        async with asyncio.timeout(self._CONNECT_TIMEOUT):
            # 1-RTT (gather) instead of 2 sequential RTTs — both calls
            # share the same timeout budget and target disjoint catalogs
            # (resources vs. templates), so ordering is irrelevant.
            # ``return_exceptions=True`` so a fast failure on one call
            # cannot orphan the sibling: fail-fast gather leaves the
            # survivor running DETACHED — outside this timeout scope and
            # outside the lock serialization — as an unbounded in-flight
            # request on the shared session. Both awaitables complete
            # here (the timeout cancels them together on expiry), then
            # the first failure is re-raised.
            pool_res_pair: tuple[Any, Any] = await asyncio.gather(
                session.list_resources(),
                session.list_resource_templates(),
                return_exceptions=True,
            )
            res_result, tmpl_result = pool_res_pair
        pool_res_exc: BaseException | None = next(
            (r for r in (res_result, tmpl_result) if isinstance(r, BaseException)), None
        )
        if pool_res_exc is not None:
            raise pool_res_exc
        assert not isinstance(res_result, BaseException)
        assert not isinstance(tmpl_result, BaseException)
        if self._user_pool_entries.get(key) is not entry:
            # Entry replaced mid-flight — stale result, discard.
            return [], []

        server_resources: list[dict[str, Any]] = []
        for r in _cap_server_resources(server_name, res_result.resources):
            server_resources.append(
                {
                    "uri": str(r.uri),
                    "name": r.name or "",
                    "description": r.description or "",
                    "mimeType": r.mimeType or "",
                    "server": server_name,
                }
            )
        for t in _cap_server_resource_templates(server_name, tmpl_result.resourceTemplates):
            server_resources.append(
                {
                    "uri": str(t.uriTemplate),
                    "name": t.name or "",
                    "description": t.description or "",
                    "mimeType": t.mimeType or "",
                    "server": server_name,
                    "template": True,
                }
            )

        new_uris = {r["uri"] for r in server_resources if not r.get("template")}
        entry.resources = server_resources
        self._rebuild_user_resource_map(user_id)
        # Fire user-keyed AND admin (None) listeners. Other users'
        # listeners do NOT see this change — the pool catalog is private.
        self._notify_user_resource_listeners(user_id)

        added = sorted(new_uris - old_uris)
        removed = sorted(old_uris - new_uris)
        if added or removed:
            log.info(
                "Refreshed pool MCP server user=%s server=%s: +%d/-%d resource(s)",
                user_id,
                server_name,
                len(added),
                len(removed),
            )
        return added, removed

    async def _refresh_pool_server_prompts(
        self, key: tuple[str, str]
    ) -> tuple[list[str], list[str]]:
        """Re-fetch prompts for one pool entry. Returns ``(added, removed)`` names.

        Mirror of :meth:`_refresh_pool_server_tools` for the prompt
        path (RFC §3.3). Skips when ``supports_prompts`` is False so a
        server that no longer advertises prompts doesn't trigger a list
        call. ``asyncio.timeout`` is mandatory — a wedged server must
        not hang the refresh (and the lock it holds) forever.

        MUST run on the mcp-loop, with the entry's ``open_lock`` HELD
        (:meth:`_run_notification_refresh` acquires it): an unlocked
        refresh races a connect's discovery wiring and sibling
        refreshes, either of which can publish an older catalog over
        this one — see the runner's ordering rationale.
        """
        entry = self._user_pool_entries.get(key)
        if entry is None or entry.session is None or not entry.supports_prompts:
            return [], []
        session = entry.session
        user_id, server_name = key
        old_names = {p["name"] for p in (entry.prompts or [])}

        async with asyncio.timeout(self._CONNECT_TIMEOUT):
            prompt_result = await session.list_prompts()
        if self._user_pool_entries.get(key) is not entry:
            # Entry replaced mid-flight — stale result, discard.
            return [], []

        server_prompts: list[dict[str, Any]] = []
        for p in _cap_server_prompts(server_name, prompt_result.prompts):
            server_prompts.append(
                {
                    "name": f"mcp__{server_name}__{p.name}",
                    "original_name": p.name,
                    "server": server_name,
                    "description": p.description or "",
                    "arguments": [
                        {
                            "name": a.name,
                            "description": a.description or "",
                            "required": a.required or False,
                        }
                        for a in (p.arguments or [])
                    ],
                }
            )

        new_names = {p["name"] for p in server_prompts}
        entry.prompts = server_prompts
        self._rebuild_user_prompt_map(user_id)
        self._notify_user_prompt_listeners(user_id)

        added = sorted(new_names - old_names)
        removed = sorted(old_names - new_names)
        if added or removed:
            log.info(
                "Refreshed pool MCP server user=%s server=%s: +%d/-%d prompt(s)",
                user_id,
                server_name,
                len(added),
                len(removed),
            )
        return added, removed

    async def _refresh_server(self, name: str) -> tuple[list[str], list[str]] | None:
        """Re-fetch tools, resources, and prompts for one server.

        Returns ``(added_tools, removed_tools)`` names (tool diff only,
        for backward compatibility with ``/mcp refresh`` output), or
        ``None`` when the pass was SUPERSEDED — the server was removed
        (or removed and re-added) while this pass waited on the lock, so
        its mandate is gone and it must publish nothing and write no
        status: the removal cleaned the status maps, and a re-add's
        connect discovery owns the new generation's status. Writing
        anything here would either resurrect rows for a nonexistent
        server or stamp a false ``ok`` over a generation this pass never
        actually refreshed.

        Writes the ``_last_refresh`` entry on every call so the Phase 9
        admin status pill reflects every refresh path — manual
        operator-driven ``refresh_sync`` AND the ``_cb_auto_reconnect``
        follow-up that schedules ``_refresh_server`` directly.
        Centralising the write here means future schedule sites
        automatically populate the field.

        Uses ``asyncio.gather(return_exceptions=True)`` so that a failure
        in one of the three concurrent sub-refreshes does NOT orphan the
        others mid-mutation: every sibling reaches completion (success
        or per-task failure) before the outcome is computed.  The
        ``_last_refresh`` write is ``"ok"`` iff all three succeeded; on
        any failure the outcome is ``f"error:{type(first_exc).__name__}"``
        and the first exception is re-raised so the outer caller's error
        path (``_refresh_all``'s except, or the manual-refresh sync
        wrapper) sees the same shape it did before this rework.
        Partial-success mutations of ``state.tools`` / ``state.resources``
        / ``state.prompts`` are bounded to whichever sub-refresh
        succeeded — the documented trade-off vs leaving orphan tasks
        running after the error is observed.

        Serialized on the per-name connect lock: every publisher of a
        static per-server catalog — a connect's discovery wiring, a
        spawned notification refresh
        (:meth:`_run_static_notification_refresh`), and this
        manual/periodic pass — writes under the same lock, so each list
        call is issued only after the previous publisher finished and
        the last publish is always the freshest. An unserialized pass
        could land its older snapshot over a notification refresh's
        newer one, with no convergence until the next change. No caller
        holds the lock coming in: ``_refresh_all`` takes it per-branch
        (never nested), and the two post-reconnect schedule sites spawn
        this as its own task (through
        :meth:`_refresh_server_logged` — the raise below must never
        reach ``_spawn_background``'s ``exc_info`` failure log).

        The post-acquire recheck closes the remove → re-add race the
        same way :meth:`_ensure_static_connected` does — by LOCK
        IDENTITY: ``remove_server_sync`` retires the lock object after
        teardown, so a pass that parked on the OLD lock must not run
        its list calls concurrently with a re-add publishing under the
        NEW lock (two unserialized publishers — the exact race this
        lock exists to close). The get-or-create acquire mirrors
        ``_ensure_static_connected``; a mint for a just-removed name is
        bounded (a re-add reuses it, shutdown clears it) and the
        recheck below keeps it inert.
        """
        lock = self._static_connect_lock_for(name)
        async with lock:
            if (
                self._static_connect_locks.get(name) is not lock
                or self._static_servers.get(name) is None
            ):
                # Superseded while parked: removed (state gone), or
                # removed + re-added (lock retired — the new generation
                # publishes its own discovery under the NEW lock).
                return None
            results = await asyncio.gather(
                self._refresh_server_tools(name),
                self._refresh_server_resources(name),
                self._refresh_server_prompts(name),
                return_exceptions=True,
            )
            first_exc: BaseException | None = next(
                (r for r in results if isinstance(r, BaseException)), None
            )
            if first_exc is not None:
                self._last_refresh[name] = (
                    time.time(),
                    f"error:{type(first_exc).__name__}",
                )
                raise first_exc
            tool_diff = results[0]
            # ``return_exceptions=True`` widens the static type; on the all-
            # success path each entry is the awaited result.  We narrow the
            # tool-diff entry to the documented ``(added, removed)`` shape.
            assert isinstance(tool_diff, tuple)
            added, removed = tool_diff
            self._last_error.pop(name, None)
            self._last_refresh[name] = (time.time(), "ok")
            return added, removed

    async def _refresh_server_logged(self, name: str) -> None:
        """:meth:`_refresh_server` for ``_spawn_background`` schedule sites.

        The spawned pass has no caller to observe the re-raise, so an
        escaping exception would land in ``_spawn_background``'s
        done-callback, whose ``exc_info`` log serializes the chained
        ``httpx.Request`` — headers carrying the configured bearer for
        ``auth_type=static`` servers. Swallow here with the same
        ``type: str(exc)`` shape as :meth:`_refresh_all`'s except;
        ``_refresh_server`` already wrote the ``_last_refresh`` error
        row before re-raising, so no outcome is lost — only the leak.
        """
        try:
            await self._refresh_server(name)
        except (Exception, BaseExceptionGroup) as exc:
            log.warning(
                "Post-reconnect catalog refresh failed for '%s' exc=%s: %s",
                name,
                type(exc).__name__,
                exc,
            )
            self._set_error(name, f"Refresh failed: {type(exc).__name__}: {exc}")

    async def _refresh_all(
        self, server_name: str | None = None
    ) -> dict[str, tuple[list[str], list[str]]]:
        """Refresh tools, resources, and prompts for one or all servers.

        For disconnected servers (in config but not connected), attempts
        reconnect.  Returns ``{server: (added, removed)}`` per server.
        """
        results: dict[str, tuple[list[str], list[str]]] = {}
        targets = [server_name] if server_name else list(self._server_configs.keys())

        for name in targets:
            try:
                state = self._static_servers.get(name)
                if state is None or state.session is None:
                    # Attempt reconnect via the shared lazy primitive — it owns
                    # the per-name lock, live-session reuse, the in_flight
                    # deferral, and the breaker (so no separate
                    # ``_cb_record_success`` here: only the open-circuit
                    # deadline is cleared; a real dispatch resets the count).
                    cfg = self._server_configs.get(name)
                    if cfg:
                        log.info("Reconnecting MCP server '%s'", name)
                        session = await self._ensure_static_connected(name, cfg)
                        if session is None:
                            # Deliberate skip: removed concurrently, or a
                            # sibling call is still in flight on the old
                            # stack. Not a failure — the next refresh or
                            # health tick retries.
                            results[name] = ([], [])
                            continue
                        post = self._static_servers.get(name)
                        new_names = (
                            [t["function"]["name"] for t in post.tools] if post is not None else []
                        )
                        results[name] = (new_names, [])
                        self._last_refresh[name] = (time.time(), "ok")
                    continue
                refreshed = await self._refresh_server(name)
                if refreshed is None:
                    # Superseded (removed / removed+re-added while parked
                    # on the lock) — a deliberate skip, not an outcome:
                    # nothing was refreshed, so record neither success
                    # nor failure for whatever generation lives now.
                    results[name] = ([], [])
                    continue
                added, removed = refreshed
                self._cb_record_success(name)
                results[name] = (added, removed)
            except (Exception, BaseExceptionGroup) as exc:
                # BaseExceptionGroup: a transport task-group failure from the
                # reconnect/refresh must stay isolated to this server, not
                # abort the whole refresh pass.
                #
                # ``type: str(exc)``, never ``exc_info`` — the serialized
                # exception CHAIN carries the chained ``httpx.Request``
                # whose headers hold the configured bearer for
                # ``auth_type=static`` servers; the message text is
                # diagnostic and header-free.
                log.warning(
                    "Refresh failed for MCP server '%s' exc=%s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                )
                self._set_error(name, f"Refresh failed: {type(exc).__name__}: {exc}")
                results[name] = ([], [])
                # A dead transport leaves a non-None but unusable session, so
                # the reconnect branch at the top of this loop (gated on
                # ``session is None``) would never fire and we would re-probe
                # the corpse on every tick forever — the exact failure that
                # required a full process restart to clear. Evicting the
                # session here makes the NEXT refresh tick reconnect, turning
                # this periodic refresh into a self-healing liveness probe.
                if _is_dead_transport(exc):
                    dead_state = self._static_servers.get(name)
                    if dead_state is not None:
                        self._drop_static_session_and_stamp(name, dead_state)
                # Overwrite unconditionally with the freshest observed
                # outcome.  Two cases produce the write:
                # (1) Reconnect branch: ``_connect_one`` raised before
                #     ``_refresh_server`` could write — no prior entry
                #     from this iteration exists yet, so the write is
                #     the only fresh signal.
                # (2) ``_refresh_server`` branch: it already wrote a
                #     fresh ``error:<ClassName>`` before re-raising, so
                #     the outer overwrite is a no-op for the value.
                # Using ``setdefault`` here would preserve a stale prior
                # ``"ok"`` from the previous successful refresh when the
                # current attempt fails — the admin pill would show
                # "ok" for a broken server.
                self._last_refresh[name] = (time.time(), f"error:{type(exc).__name__}")

        # Final sync to clean up templates from servers that are no longer connected
        try:
            self.sync_prompts_to_storage()
        except Exception:
            log.warning("Prompt sync after refresh_all failed", exc_info=True)

        return results

    def refresh_sync(
        self, server_name: str | None = None, timeout: int = 30
    ) -> dict[str, tuple[list[str], list[str]]]:
        """Refresh tools synchronously (blocks the calling thread).

        Returns ``{server: (added_names, removed_names)}`` per server.
        """
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(self._refresh_all(server_name), self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"MCP refresh timed out after {timeout}s") from None

    # -- resource refresh ----------------------------------------------------

    def _rebuild_resources(self) -> None:
        """Rebuild merged ``_resources`` and ``_resource_map`` from per-server state.

        Uses copy-on-write: builds new objects, then assigns atomically.
        """
        new_resources: list[dict[str, Any]] = []
        new_map: dict[str, tuple[str, str]] = {}
        for srv_name, srv_state in self._static_servers.items():
            for res in srv_state.resources:
                uri: str = res["uri"]
                new_resources.append(res)
                if res.get("template"):
                    continue  # templates are catalog-only, not directly readable
                if uri in new_map:
                    log.warning(
                        "Resource URI collision: '%s' from '%s' overrides '%s'",
                        uri,
                        srv_name,
                        new_map[uri][0],
                    )
                new_map[uri] = (srv_name, uri)
        # Build template prefix map for URI expansion fallback
        new_prefixes: dict[str, tuple[str, str]] = {}
        for srv_name, srv_state in self._static_servers.items():
            for res in srv_state.resources:
                if res.get("template"):
                    tmpl_uri = res["uri"]
                    brace = tmpl_uri.find("{")
                    prefix = tmpl_uri[:brace] if brace >= 0 else tmpl_uri
                    if prefix:
                        if prefix in new_prefixes:
                            existing_srv, existing_tmpl = new_prefixes[prefix]
                            if len(tmpl_uri) > len(existing_tmpl):
                                log.warning(
                                    "Template prefix collision: '%s' from '%s' overrides '%s'"
                                    " (keeping more specific template)",
                                    prefix,
                                    srv_name,
                                    existing_srv,
                                )
                                new_prefixes[prefix] = (srv_name, tmpl_uri)
                            else:
                                log.warning(
                                    "Template prefix collision: '%s' from '%s' ignored in"
                                    " favor of '%s' (keeping more specific template)",
                                    prefix,
                                    srv_name,
                                    existing_srv,
                                )
                        else:
                            new_prefixes[prefix] = (srv_name, tmpl_uri)

        self._resources = new_resources
        self._resource_map = new_map
        self._template_prefixes = new_prefixes
        self._notify_resource_listeners()

    async def _refresh_server_resources(self, name: str) -> None:
        """Re-fetch resources for one server."""
        state = self._static_servers.get(name)
        if state is None or not state.supports_resources:
            return
        if state.session is None:
            return
        # Capture session locally — a concurrent transport-error eviction in
        # call_tool_sync can clear state.session between awaits, which would
        # turn the second list_resource_templates() call into AttributeError.
        session = state.session

        # ``asyncio.timeout`` mandatory — a wedged server must not hang a
        # spawned refresh (and the connect lock it holds) forever.
        async with asyncio.timeout(self._CONNECT_TIMEOUT):
            # 1-RTT (gather) instead of 2 sequential RTTs — both calls
            # share the same timeout budget and target disjoint catalogs
            # (resources vs. templates), so ordering is irrelevant.
            # ``return_exceptions=True`` so a fast failure on one call
            # cannot orphan the sibling as a detached, unbounded request
            # on the shared session — see
            # :meth:`_refresh_pool_server_resources`.
            static_res_pair: tuple[Any, Any] = await asyncio.gather(
                session.list_resources(),
                session.list_resource_templates(),
                return_exceptions=True,
            )
            res_result, tmpl_result = static_res_pair
        static_res_exc: BaseException | None = next(
            (r for r in (res_result, tmpl_result) if isinstance(r, BaseException)), None
        )
        if static_res_exc is not None:
            raise static_res_exc
        assert not isinstance(res_result, BaseException)
        assert not isinstance(tmpl_result, BaseException)
        if self._static_servers.get(name) is not state:
            # Entry replaced (remove + re-add) mid-flight — stale result.
            return

        server_resources: list[dict[str, Any]] = []
        for r in res_result.resources:
            server_resources.append(
                {
                    "uri": str(r.uri),
                    "name": r.name or "",
                    "description": r.description or "",
                    "mimeType": r.mimeType or "",
                    "server": name,
                }
            )
        for t in tmpl_result.resourceTemplates:
            server_resources.append(
                {
                    "uri": str(t.uriTemplate),
                    "name": t.name or "",
                    "description": t.description or "",
                    "mimeType": t.mimeType or "",
                    "server": name,
                    "template": True,
                }
            )

        state.resources = server_resources
        self._rebuild_resources()

    # -- prompt refresh ------------------------------------------------------

    def _rebuild_prompts(self) -> None:
        """Rebuild merged ``_prompts`` and ``_prompt_map`` from per-server state.

        Uses copy-on-write: builds new objects, then assigns atomically.
        """
        new_prompts: list[dict[str, Any]] = []
        new_map: dict[str, tuple[str, str]] = {}
        for srv_name, srv_state in self._static_servers.items():
            for prompt in srv_state.prompts:
                prefixed: str = prompt["name"]
                new_prompts.append(prompt)
                new_map[prefixed] = (srv_name, prompt["original_name"])
        self._prompts = new_prompts
        self._prompt_map = new_map
        self._notify_prompt_listeners()

    async def _refresh_server_prompts(self, name: str) -> None:
        """Re-fetch prompts for one server."""
        state = self._static_servers.get(name)
        if state is None or not state.supports_prompts:
            return
        if state.session is None:
            return
        # Capture session locally — see _refresh_server_resources for the
        # concurrent-eviction race this guards against. Single-await today,
        # multi-await tomorrow; consistent capture-once idiom.
        session = state.session

        # ``asyncio.timeout`` mandatory — a wedged server must not hang a
        # spawned refresh (and the connect lock it holds) forever.
        async with asyncio.timeout(self._CONNECT_TIMEOUT):
            prompt_result = await session.list_prompts()
        if self._static_servers.get(name) is not state:
            # Entry replaced (remove + re-add) mid-flight — stale result.
            return

        server_prompts: list[dict[str, Any]] = []
        for p in prompt_result.prompts:
            server_prompts.append(
                {
                    "name": f"mcp__{name}__{p.name}",
                    "original_name": p.name,
                    "server": name,
                    "description": p.description or "",
                    "arguments": [
                        {
                            "name": a.name,
                            "description": a.description or "",
                            "required": a.required or False,
                        }
                        for a in (p.arguments or [])
                    ],
                }
            )

        state.prompts = server_prompts
        self._rebuild_prompts()

        # Sync discovered prompts into governance storage
        try:
            self.sync_prompts_to_storage()
        except Exception:
            log.warning("Prompt sync after refresh failed for '%s'", name, exc_info=True)

    # -- listener infrastructure ---------------------------------------------

    def add_listener(self, callback: Callable[[], None], *, user_id: str | None = None) -> None:
        """Register a callback invoked when the tool list changes.

        ``user_id=None`` (default) registers a global / admin listener
        that fires on every tool-change (static or pool). A string
        ``user_id`` scopes the listener: it fires on global static-path
        changes AND on pool changes for that user only — never on
        another user's pool change. RFC §3.3.
        """
        with self._listeners_lock:
            self._listeners.append((user_id, callback))

    def remove_listener(self, callback: Callable[[], None], *, user_id: str | None = None) -> None:
        """Unregister a tool-change callback.

        ``user_id`` MUST match the value used at registration; the
        ``(user_id, callback)`` pair is the listener identity.
        """
        with self._listeners_lock, contextlib.suppress(ValueError):
            self._listeners.remove((user_id, callback))

    def _notify_listeners(self) -> None:
        """Static-path tool change — fires ALL registered listeners.

        The static catalog is a process-wide concern: a static-server
        notification (or reconcile) updates ``_tool_map`` which every
        user-scoped session relies on, so the fan-out is unconditional.
        """
        with self._listeners_lock:
            listeners = list(self._listeners)
        for _uid, cb in listeners:
            try:
                cb()
            except Exception:
                log.warning("Tool-change listener raised", exc_info=True)

    def _notify_user_tool_listeners(self, user_id: str) -> None:
        """Pool-entry tool change — fires only listeners that should see it.

        A pool catalog change touches one user's view; firing every
        registered listener would broadcast that user's tool set to
        unrelated sessions. Scoped fan-out targets the matching
        ``user_id`` AND admin (``None``) listeners. RFC §3.3.
        """
        with self._listeners_lock:
            listeners = [cb for uid, cb in self._listeners if uid == user_id or uid is None]
        for cb in listeners:
            try:
                cb()
            except Exception:
                log.warning("Tool-change listener raised", exc_info=True)

    def add_resource_listener(
        self, callback: Callable[[], None], *, user_id: str | None = None
    ) -> None:
        """Register a callback invoked when the resource list changes.

        ``user_id=None`` (default) registers a global / admin listener
        that fires on every resource-change (static or pool). A string
        ``user_id`` scopes the listener: it fires on global static-path
        changes AND on pool changes for that user only — never on
        another user's pool change. RFC §3.2 (resources).
        """
        with self._resource_listeners_lock:
            self._resource_listeners.append((user_id, callback))

    def remove_resource_listener(
        self, callback: Callable[[], None], *, user_id: str | None = None
    ) -> None:
        """Unregister a resource-change callback.

        ``user_id`` MUST match the value used at registration; the
        ``(user_id, callback)`` pair is the listener identity.
        """
        with self._resource_listeners_lock, contextlib.suppress(ValueError):
            self._resource_listeners.remove((user_id, callback))

    def _notify_resource_listeners(self) -> None:
        """Static-path resource change — fires ALL registered listeners.

        Mirrors ``_notify_listeners`` for tools: the static catalog is
        process-wide so the fan-out is unconditional. RFC §3.2 (resources).
        """
        with self._resource_listeners_lock:
            listeners = list(self._resource_listeners)
        for _uid, cb in listeners:
            try:
                cb()
            except Exception:
                log.warning("Resource-change listener raised", exc_info=True)

    def _notify_user_resource_listeners(self, user_id: str) -> None:
        """Pool-entry resource change — fires only listeners that should see it.

        Scoped fan-out targets the matching ``user_id`` AND admin
        (``None``) listeners. Mirrors ``_notify_user_tool_listeners``.
        RFC §3.2 (resources).
        """
        with self._resource_listeners_lock:
            listeners = [
                cb for uid, cb in self._resource_listeners if uid == user_id or uid is None
            ]
        for cb in listeners:
            try:
                cb()
            except Exception:
                log.warning("Resource-change listener raised", exc_info=True)

    def add_prompt_listener(
        self, callback: Callable[[], None], *, user_id: str | None = None
    ) -> None:
        """Register a callback invoked when the prompt list changes.

        ``user_id=None`` (default) registers a global / admin listener
        that fires on every prompt-change (static or pool). A string
        ``user_id`` scopes the listener: it fires on global static-path
        changes AND on pool changes for that user only — never on
        another user's pool change. RFC §3.3.
        """
        with self._prompt_listeners_lock:
            self._prompt_listeners.append((user_id, callback))

    def remove_prompt_listener(
        self, callback: Callable[[], None], *, user_id: str | None = None
    ) -> None:
        """Unregister a prompt-change callback.

        ``user_id`` MUST match the value used at registration; the
        ``(user_id, callback)`` pair is the listener identity.
        """
        with self._prompt_listeners_lock, contextlib.suppress(ValueError):
            self._prompt_listeners.remove((user_id, callback))

    def _notify_prompt_listeners(self) -> None:
        """Static-path prompt change — fires ALL registered listeners.

        Mirrors ``_notify_listeners`` for tools: the static catalog is
        process-wide so the fan-out is unconditional. RFC §3.3 (prompts).
        """
        with self._prompt_listeners_lock:
            listeners = list(self._prompt_listeners)
        for _uid, cb in listeners:
            try:
                cb()
            except Exception:
                log.warning("Prompt-change listener raised", exc_info=True)

    def _notify_user_prompt_listeners(self, user_id: str) -> None:
        """Pool-entry prompt change — fires only listeners that should see it.

        Scoped fan-out targets the matching ``user_id`` AND admin
        (``None``) listeners. Mirrors ``_notify_user_tool_listeners``.
        RFC §3.3 (prompts).
        """
        with self._prompt_listeners_lock:
            listeners = [cb for uid, cb in self._prompt_listeners if uid == user_id or uid is None]
        for cb in listeners:
            try:
                cb()
            except Exception:
                log.warning("Prompt-change listener raised", exc_info=True)

    # -- governance storage sync ---------------------------------------------

    def set_storage(self, storage: Any) -> None:
        """Inject governance storage backend for prompt template sync.

        If MCP servers are already connected, triggers an immediate sync
        so prompts discovered during startup appear in governance storage
        (``start()`` completes before ``set_storage()`` is called).
        """
        self._storage = storage
        if self._connected.is_set():
            try:
                self.sync_prompts_to_storage()
            except Exception:
                log.warning("Prompt sync after set_storage failed", exc_info=True)

    def sync_prompts_to_storage(self) -> dict[str, Any]:
        """Sync discovered MCP prompts into the prompt_templates governance table.

        Returns ``{"added": [...], "removed": [...], "skipped": [...]}``.
        Thread-safe: serialized via ``_sync_lock`` to prevent races
        between ``set_storage()`` (main thread) and MCP background thread.
        """
        if self._storage is None:
            return {"added": [], "removed": [], "skipped": []}

        with self._sync_lock:
            return self._sync_prompts_locked()

    def _sync_prompts_locked(self) -> dict[str, Any]:
        """Inner sync logic — must be called under ``_sync_lock``."""
        storage = self._storage
        added: list[str] = []
        removed: list[str] = []
        skipped: list[str] = []

        # Current MCP prompt names (the prefixed names used as template names)
        current_names: set[str] = set()

        for prompt in list(self._prompts):
            name: str = prompt["name"][:256]
            server: str = prompt["server"][:128]
            current_names.add(name)

            # Build content from description + argument schema
            desc = prompt.get("description", "")[:4096]
            args_list = prompt.get("arguments", [])
            content_parts = [desc] if desc else []
            if args_list:
                content_parts.append("\nArguments:")
                for arg in args_list:
                    req = " (required)" if arg.get("required") else ""
                    arg_desc = arg.get("description", "")[:512]
                    content_parts.append(f"  - {arg['name'][:128]}{req}: {arg_desc}")
            content = "\n".join(content_parts) if content_parts else name

            # Variables = JSON list of argument names
            variables = json.dumps([a["name"] for a in args_list])

            existing = storage.get_prompt_template_by_name(name)
            if existing is not None:
                if existing.get("origin") == "manual":
                    log.info(
                        "Skipping MCP prompt '%s' — manual template with same name exists", name
                    )
                    skipped.append(name)
                    continue
                # Existing MCP template — update content/variables.
                # Reset is_default to prevent a compromised MCP server from
                # injecting content into a previously admin-promoted default.
                storage.update_prompt_template(
                    existing["template_id"],
                    content=content,
                    variables=variables,
                    is_default=False,
                    token_estimate=len(content) // 4,
                )
            else:
                # Create new MCP-sourced template.  Thread the prompt's
                # upstream description through so the row satisfies the
                # non-empty-description invariant; fall back to a
                # synthetic marker when the MCP server didn't provide
                # one.
                template_id = str(uuid.uuid4())
                template_description = desc.strip() or f"MCP prompt {name} from {server}"
                storage.create_prompt_template(
                    template_id=template_id,
                    name=name,
                    category="mcp",
                    content=content,
                    variables=variables,
                    is_default=False,
                    org_id="",
                    created_by="",
                    origin="mcp",
                    mcp_server=server,
                    readonly=True,
                    description=template_description,
                    activation="named",
                    token_estimate=len(content) // 4,
                )
                added.append(name)

        # Remove MCP templates whose prompts no longer exist
        existing_mcp = storage.list_prompt_templates_by_origin("mcp")
        for tpl in existing_mcp:
            if tpl["name"] not in current_names:
                storage.delete_prompt_template(tpl["template_id"])
                removed.append(tpl["name"])

        if added or removed:
            log.info(
                "MCP prompt sync: +%d added, -%d removed, %d skipped",
                len(added),
                len(removed),
                len(skipped),
            )
        return {"added": added, "removed": removed, "skipped": skipped}

    # -- lifecycle (shutdown) ------------------------------------------------

    def shutdown(self) -> None:
        """Close all MCP sessions and stop the background loop."""
        # Cancel tracked background tasks (catalog refreshes etc.) FIRST —
        # they are pure auxiliaries, and draining them up front means the
        # stack teardown below can't race an in-flight refresh.  Submitted
        # whenever a loop exists — NOT gated on a main-thread truthiness
        # check of ``_background_tasks``: a spawn queued via
        # call_soon_threadsafe may not have reached the set yet, but ready
        # callbacks run in FIFO order, so by the time the drain coroutine
        # snapshots the set ON the loop, every earlier-queued spawn has
        # landed.  ``is_running()`` guard: on a stopped loop nothing can
        # execute the drain — submitting would just stall on the future.
        if self._loop is not None and self._loop.is_running():

            async def _cancel_background() -> None:
                # The token-freshness sweep is a dedicated handle (not in
                # _background_tasks); cancel it here so a deployment that never
                # creates a pool entry — and thus skips the pool-teardown block
                # below — still stops it cleanly.
                if self._user_token_sweep_task is not None:
                    self._user_token_sweep_task.cancel()
                    with contextlib.suppress(BaseException):
                        await self._user_token_sweep_task
                    self._user_token_sweep_task = None
                # Static health loop is a dedicated handle (not in
                # _background_tasks); cancel it here so a static-only deployment
                # — which skips the pool-teardown block below — still stops it.
                if self._static_health_task is not None:
                    self._static_health_task.cancel()
                    with contextlib.suppress(BaseException):
                        await self._static_health_task
                    self._static_health_task = None
                tasks = list(self._background_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            future = asyncio.run_coroutine_threadsafe(_cancel_background(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                log.debug("Error cancelling MCP background tasks", exc_info=True)

        # Cancel the pool eviction task, then close all pool entries
        # before tearing down static-path state. Both run on the
        # mcp-loop so dispatcher coroutines can't race them.
        if self._loop and (self._user_pool_eviction_task is not None or self._user_pool_entries):

            async def _close_all_pool() -> None:
                if self._user_pool_eviction_task is not None:
                    self._user_pool_eviction_task.cancel()
                    with contextlib.suppress(BaseException):
                        await self._user_pool_eviction_task
                    self._user_pool_eviction_task = None
                # Parallel version of ``_teardown_pool_entry``'s close protocol
                # (mirrors ``_close_all_owners`` for the static path): signal
                # every owner first, one shared graceful window, then ONE cancel
                # each for stragglers and a short drain — never a second cancel
                # (it would abandon an anyio scope exit mid-flight; the owner
                # completing solo is harmless, and the loop is stopping anyway).
                owners: list[asyncio.Task[None]] = []
                for key in list(self._user_pool_entries):
                    entry = self._user_pool_entries.get(key)
                    if entry is None:
                        continue
                    entry.drop_session()
                    owner = entry.owner_task
                    close_requested = entry.close_requested
                    entry.owner_task = None
                    entry.close_requested = None
                    if close_requested is not None:
                        close_requested.set()
                    if owner is not None and not owner.done():
                        owners.append(owner)
                    # Pre-close streams to unblock the SDK's transport tasks
                    # (anyio zero-buffer send — SDK #2147) so owners unwind fast.
                    await self._pre_close_streams(key)
                if owners:
                    _done, pending = await asyncio.wait(owners, timeout=self._OWNER_CLOSE_GRACE_S)
                    for owner in pending:
                        owner.cancel()
                    if pending:
                        _done, pending = await asyncio.wait(
                            pending, timeout=self._OWNER_CANCEL_GRACE_S / 2
                        )
                        if pending:
                            log.warning(
                                "MCP shutdown: %d pool transport owner(s) still unwinding; "
                                "left to the dying loop",
                                len(pending),
                            )
                self._user_pool_entries.clear()
                self._user_pool_last_used.clear()
                self._user_pool_locks.clear()

            future = asyncio.run_coroutine_threadsafe(_close_all_pool(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                log.debug("Error closing MCP pool sessions", exc_info=True)

        # Close all per-server transports (owner tasks own the cm stacks).
        # Parallel version of ``_teardown_static_session``'s close protocol:
        # signal every owner first, give them one shared graceful window, then
        # ONE cancel each for stragglers and a short drain — never a second
        # cancel (it would abandon an anyio scope exit mid-flight; the owner
        # completing solo is harmless, and the loop is stopping anyway).
        if self._loop and self._static_servers:

            async def _close_all_owners() -> None:
                owners: list[asyncio.Task[None]] = []
                for srv_name, srv_state in list(self._static_servers.items()):
                    self._drop_static_session_and_stamp(srv_name, srv_state)
                    owner = srv_state.owner_task
                    close_requested = srv_state.close_requested
                    srv_state.owner_task = None
                    srv_state.close_requested = None
                    if close_requested is not None:
                        close_requested.set()
                    if owner is not None and not owner.done():
                        owners.append(owner)
                    # Pre-close streams to unblock the SDK's transport tasks
                    # (anyio zero-buffer send — SDK #2147) so owners unwind fast.
                    await self._pre_close_streams(srv_name)
                if not owners:
                    return
                _done, pending = await asyncio.wait(owners, timeout=self._OWNER_CLOSE_GRACE_S)
                for owner in pending:
                    owner.cancel()
                if pending:
                    _done, pending = await asyncio.wait(
                        pending, timeout=self._OWNER_CANCEL_GRACE_S / 2
                    )
                    if pending:
                        log.warning(
                            "MCP shutdown: %d transport owner(s) still unwinding; "
                            "left to the dying loop",
                            len(pending),
                        )

            future = asyncio.run_coroutine_threadsafe(_close_all_owners(), self._loop)
            try:
                future.result(timeout=12)
            except Exception:
                log.debug("Error closing MCP sessions", exc_info=True)

        # Close legacy shared stack (if any resources were registered on it)
        if self._loop and self._exit_stack:
            future = asyncio.run_coroutine_threadsafe(self._exit_stack.aclose(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                log.debug("Error closing MCP exit stack", exc_info=True)

        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                # Closing a still-running loop raises; the daemon thread dies
                # with the process, so leaving the loop open is the lesser
                # evil.  Loud because a stuck loop thread is itself a bug.
                log.warning("MCP loop thread did not stop within 5s; loop left open")
            else:
                if self._loop is not None:
                    self._loop.close()
                self._loop = None
                self._thread = None
        # When no thread was started (tests wire ``_loop`` directly) the loop
        # is not ours to close — the stop above is all the owner needs.

        # Clear all state
        self._background_tasks.clear()
        self._static_servers.clear()
        self._db_managed.clear()
        self._tools = []
        self._tool_map = {}
        self._resources = []
        self._resource_map = {}
        self._template_prefixes = {}
        self._prompts = []
        self._prompt_map = {}
        # Clear listener lists to release callback references
        self._listeners.clear()
        self._resource_listeners.clear()
        self._prompt_listeners.clear()
        # Clear resilience state
        self._consecutive_failures.clear()
        self._circuit_open_until.clear()
        self._circuit_trip_count.clear()
        self._last_notification_refresh.clear()
        self._static_refresh_pending.clear()
        self._last_pool_notification_refresh.clear()
        self._pool_refresh_pending.clear()
        # Pool state already cleared above when the loop was alive; this
        # makes shutdown idempotent if the loop exited before pool state
        # could be wound down (e.g., manager constructed but never started).
        self._user_pool_entries.clear()
        self._user_pool_last_used.clear()
        self._user_pool_locks.clear()
        self._user_tool_map.clear()
        self._user_tools.clear()
        # Phase 7b — per-user resource / prompt catalogs.
        self._user_resource_map.clear()
        self._user_resources.clear()
        self._user_template_prefixes.clear()
        self._user_prompt_map.clear()
        self._user_prompts.clear()
        self._user_pool_eviction_task = None
        self._user_token_sweep_task = None
        self._token_sweep_warned.clear()
        self._static_health_task = None
        self._static_connect_locks.clear()
        self._static_reconnect_attempt.clear()
        self._static_reconnect_next.clear()
        self._static_next_ping.clear()

        log.info("MCP client shut down")

    # -- hot-reload (add/remove servers) ------------------------------------

    def add_server_sync(self, name: str, cfg: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        """Connect a new MCP server at runtime (blocks the calling thread).

        Returns status dict with keys: connected, tools, resources, prompts, error.

        Note on ``_oauth_user_server_names``: this method does NOT update
        the oauth_user-name cache. ``_db_servers_to_config`` strips
        ``auth_type='oauth_user'`` rows before this method is reached, so
        production callers (only :meth:`reconcile_sync`) never pass an
        oauth_user cfg here — the cache is rebuilt wholesale by
        ``reconcile_sync`` at the top of every reconcile, which is the
        canonical update point. Direct test callers passing an oauth_user
        cfg would leave the cache stale; route through ``reconcile_sync``
        instead.
        """
        if "__" in name:
            return {
                "connected": False,
                "tools": 0,
                "resources": 0,
                "prompts": 0,
                "error": f"Server name '{name}' contains '__' (reserved delimiter)",
            }
        if self._loop is None:
            return {
                "connected": False,
                "tools": 0,
                "resources": 0,
                "prompts": 0,
                "error": "MCP event loop not running",
            }

        # Add to config so _refresh_all can reconnect on failure
        self._server_configs[name] = cfg

        future = asyncio.run_coroutine_threadsafe(self._connect_one(name, cfg), self._loop)
        try:
            future.result(timeout=timeout)
        except Exception as exc:
            # Remove from configs on failure
            self._server_configs.pop(name, None)
            return {"connected": False, "tools": 0, "resources": 0, "prompts": 0, "error": str(exc)}

        state = self._static_servers.get(name)
        return {
            "connected": state is not None and state.session is not None,
            "tools": len(state.tools) if state else 0,
            "resources": len(state.resources) if state else 0,
            "prompts": len(state.prompts) if state else 0,
            "error": "",
        }

    def reconnect_sync(
        self, name: str, timeout: float = _STATIC_RECONNECT_CALLER_TIMEOUT_S
    ) -> dict[str, Any]:
        """Force a fresh connection to an MCP server (blocks the calling thread).

        Tears down the current session/transport (if any), clears the circuit
        breaker, and runs a new ``_connect_one``.  Returns status dict with
        keys: connected, tools, resources, prompts, error (parity with
        ``add_server_sync``).
        """
        if name not in self._server_configs:
            return {
                "connected": False,
                "tools": 0,
                "resources": 0,
                "prompts": 0,
                "error": "unknown server",
            }
        if self._loop is None:
            return {
                "connected": False,
                "tools": 0,
                "resources": 0,
                "prompts": 0,
                "error": "MCP event loop not running",
            }

        cfg = self._server_configs[name]

        async def _reconnect() -> None:
            self._cb_clear(name)
            # Hold the per-name connect lock across the FORCE rebuild so an
            # autonomous health-loop / dispatch reconnect can never interleave
            # its own teardown/rebuild on the shared ``StaticServerState``
            # mid-flight (delivering the ``_connect_one`` docstring's "operator
            # refresh can never interleave" claim). Call the LOCKED body
            # directly — we hold the lock, and ``_connect_one`` would deadlock
            # re-acquiring it. No explicit pre-teardown: ``_connect_one_locked``'s
            # stale-guard runs the identical ``_teardown_static_session`` first
            # thing, and an operator reconnect deliberately rebuilds even a
            # live session (unlike the lazy ``_ensure_static_connected`` path).
            async with self._static_connect_lock_for(name):
                try:
                    # Bounded connect — mirrors ``_ensure_static_connected``'s
                    # inner timeout. The attempt bound covers discovery (the
                    # one phase ``_connect_one_locked``'s handshake-only
                    # ``_CONNECT_TIMEOUT`` does not cover), fires strictly
                    # before the caller-side ``future.result(timeout=...)``,
                    # and converts to ``TimeoutError`` inside the lock so the
                    # catalog cleanup below runs instead of the connect being
                    # externally cancelled mid-flight.
                    async with asyncio.timeout(self._STATIC_RECONNECT_ATTEMPT_TIMEOUT_S):
                        await self._connect_one_locked(name, cfg)
                except BaseException:
                    # Connect failed mid-reconnect — drop the stale per-server
                    # catalog so the merged tool/resource/prompt maps don't keep
                    # advertising entries with no live session behind them, and
                    # null the session so a later health-loop check doesn't see
                    # a live (but tool-less) session.
                    fail_state = self._static_servers.get(name)
                    if fail_state is not None:
                        fail_state.tools = []
                        fail_state.resources = []
                        fail_state.prompts = []
                        self._drop_static_session_and_stamp(name, fail_state)
                    self._rebuild_tools()
                    self._rebuild_resources()
                    self._rebuild_prompts()
                    raise

        future = asyncio.run_coroutine_threadsafe(_reconnect(), self._loop)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return {
                "connected": False,
                "tools": 0,
                "resources": 0,
                "prompts": 0,
                "error": f"MCP server '{name}' reconnect timed out",
            }
        except Exception as exc:
            return {"connected": False, "tools": 0, "resources": 0, "prompts": 0, "error": str(exc)}

        state = self._static_servers.get(name)
        return {
            "connected": state is not None and state.session is not None,
            "tools": len(state.tools) if state else 0,
            "resources": len(state.resources) if state else 0,
            "prompts": len(state.prompts) if state else 0,
            "error": "",
        }

    def remove_server_sync(
        self, name: str, timeout: float = _STATIC_RECONNECT_CALLER_TIMEOUT_S
    ) -> bool:
        """Disconnect and remove an MCP server at runtime (blocks the calling thread).

        All state mutations run on the MCP event loop thread to avoid races
        with notification handlers and refresh tasks.

        Returns True if the server was connected and successfully removed.

        Note on ``_oauth_user_server_names``: this method does NOT discard
        ``name`` from the oauth_user-name cache. The cache is the source of
        truth for "this server exists in the DB as oauth_user", not "this
        server is connected on the static path" — those are separate
        concerns. A static→oauth_user transition during reconcile_sync
        keeps ``name`` in the cache (the new identity) AND calls this
        method to drop the old static-path connection; discarding here
        would silently make web_search resolve to a now-pool-only server.
        The cache is updated only by :meth:`reconcile_sync`'s wholesale
        rebuild at line 2340.
        """
        existing = self._static_servers.get(name)
        was_connected = existing is not None and existing.session is not None

        # Remove from config to prevent reconnection
        self._server_configs.pop(name, None)

        if self._loop is not None:

            async def _remove() -> None:
                # Hold the per-name connect lock across teardown so an autonomous
                # health-loop reconnect can't interleave (config was already
                # popped above, so no NEW reconnect will start; this serializes
                # against one already in flight).
                async with self._static_connect_lock_for(name):
                    # Close session + transport via the owner close protocol
                    await self._teardown_static_session(name)
                    # Clean up per-server state (on the event loop thread).
                    # The direct stamp pops back up the teardown call above:
                    # ``_teardown_static_session`` early-returns (no pop) when
                    # the state entry is already gone. The marker discards
                    # keep a parked old-generation runner's marker from
                    # coalescing AWAY a re-added server's first push — that
                    # runner bails at its lock-identity check without
                    # refreshing, so nothing would cover the dropped change.
                    self._static_servers.pop(name, None)
                    self._last_error.pop(name, None)
                    for kind in _LIST_CHANGED_KINDS.values():
                        self._last_notification_refresh.pop((name, kind), None)
                        self._static_refresh_pending.discard((name, kind))
                    self._cb_clear(name)
                    # Clear health-loop backoff/ping state so a later re-add of
                    # the same name doesn't inherit stale ``due`` deadlines.
                    self._static_reconnect_attempt.pop(name, None)
                    self._static_reconnect_next.pop(name, None)
                    self._static_next_ping.pop(name, None)
                    # Rebuild merged state (serialized with notification handlers)
                    self._rebuild_tools()
                    self._rebuild_resources()
                    self._rebuild_prompts()
                # Drop the now-orphaned per-name lock AFTER releasing it (the
                # server is gone; a re-add re-creates it lazily).
                self._static_connect_locks.pop(name, None)

            future = asyncio.run_coroutine_threadsafe(_remove(), self._loop)
            try:
                future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                # A slow reconnect held the per-name lock past our wait. CANCEL
                # the pending ``_remove`` so it can't later pop a re-added entry
                # (or its new lock) and corrupt state; report failure rather than
                # a false "removed" (the default timeout exceeds the reconnect
                # bound, so this is an edge — a caller passing a short timeout).
                future.cancel()
                log.warning("MCP server '%s' removal timed out; cancelled", name)
                return False
            except Exception:
                log.warning("Error removing MCP server '%s'", name, exc_info=True)
                return False
        else:
            # No event loop (tests / pre-start) — mutate directly
            self._static_servers.pop(name, None)
            self._last_error.pop(name, None)
            for kind in _LIST_CHANGED_KINDS.values():
                self._last_notification_refresh.pop((name, kind), None)
                self._static_refresh_pending.discard((name, kind))
            self._cb_clear(name)
            self._rebuild_tools()
            self._rebuild_resources()
            self._rebuild_prompts()

        # Clean up governance templates from this server
        try:
            self.sync_prompts_to_storage()
        except Exception:
            log.warning("Prompt sync after remove failed for '%s'", name, exc_info=True)

        log.info("Removed MCP server '%s'", name)
        return was_connected

    def _set_error(self, name: str, msg: str) -> None:
        """Store a sanitized error string for a server."""
        clean = msg.replace("\n", " ").replace("\r", "")
        self._last_error[name] = clean[: self._MAX_ERROR_LEN]

    def get_server_status(
        self, name: str, user_id: str | None = None, *, aggregate: bool = False
    ) -> dict[str, Any]:
        """Return live status for a single server, including config details.

        For ``auth_type='oauth_user'`` servers the result is scoped to *user_id*,
        or aggregated across all users when ``aggregate=True`` (see
        :meth:`_oauth_user_server_status`). Both are ignored for static servers,
        whose session is process-global.
        """
        # Pool-backed servers (oauth_user / oauth_obo) hold NO process-global
        # session — they are warmed per-user into the pool — so the
        # static-session check below would always report them "connecting".
        # Derive their status from the REQUESTING user's warm pool entry
        # instead, so the console pill reflects that user's real reachability
        # once their pool is primed.
        if self._is_pool_server(name):
            return self._oauth_user_server_status(name, user_id, aggregate=aggregate)
        state = self._static_servers.get(name)
        connected = state is not None and state.session is not None
        cfg = self._server_configs.get(name, {})
        transport = cfg.get("type", "stdio")
        cb_deadline = self._circuit_open_until.get(name)
        cb_open = cb_deadline is not None and time.monotonic() < cb_deadline
        last_refresh = self._last_refresh.get(name)
        # Inline predicate (instead of reusing ``connected``) so mypy narrows
        # ``state`` for the attribute reads — a separate boolean wouldn't.
        return {
            "connected": connected,
            "tools": len(state.tools) if state is not None and state.session is not None else 0,
            "resources": (
                len(state.resources) if state is not None and state.session is not None else 0
            ),
            "prompts": len(state.prompts) if state is not None and state.session is not None else 0,
            "error": self._last_error.get(name, ""),
            "transport": transport,
            "command": cfg.get("command", "") if transport == "stdio" else "",
            "url": cfg.get("url", "") if transport != "stdio" else "",
            "circuit_open": cb_open,
            "consecutive_failures": self._consecutive_failures.get(name, 0),
            # Phase 9 admin status: last manual / auto-reconnect refresh.
            # ``null`` when no refresh has occurred since process start.
            "last_refresh_at": last_refresh[0] if last_refresh is not None else None,
            "last_refresh_outcome": last_refresh[1] if last_refresh is not None else None,
        }

    def _oauth_user_server_status(
        self, name: str, user_id: str | None, *, aggregate: bool = False
    ) -> dict[str, Any]:
        """Live status for an ``auth_type='oauth_user'`` server, scoped to *user_id*.

        These have no global session (stripped from ``_server_configs`` /
        ``_static_servers``); they connect per-user into ``_user_pool_entries``.
        ``connected`` and the catalog counts reflect ONLY the requesting user's
        warm pool entry — never another user's. The per-user pool is per-user
        data, and ``connected`` / ``tools`` / ``resources`` / ``prompts`` reach
        read-scoped callers over the wire, so deriving them from an arbitrary
        other user's pool would leak that user's catalog (and its existence) to
        anyone with read scope. A request with no user context (``user_id``
        falsy, e.g. an operator refresh/reconnect) reports ``connected=False``.

        ``aggregate=True`` is the admin cluster-health view: ``connected`` and a
        representative catalog count reflect ANY user's warm pool. It is gated at
        the endpoint on the ``admin.mcp`` permission, whose holders already see
        cross-user MCP state (consent counts, server config), so it is not a new
        disclosure — it restores the "in use by anyone" pill for operators
        without exposing one user's pool to another read-scoped user.
        """
        # Snapshot with list(): the mcp-loop thread mutates _user_pool_entries
        # (prime insert / idle eviction) concurrently with status polls from the
        # console/server thread, and iterating a live dict that changes size
        # raises RuntimeError mid-comprehension. The sibling get_all_server_status
        # and the eviction loop snapshot the same way.
        entries = list(self._user_pool_entries.items())
        if aggregate:
            scoped = [e for (uid, sname), e in entries if sname == name]
        elif user_id:
            scoped = [e for (uid, sname), e in entries if sname == name and uid == user_id]
        else:
            scoped = []
        warm = [e for e in scoped if e.session is not None]
        # Cooled entries (transport idled out, catalog retained for the
        # user's live sessions, #836) still SERVE their tools — a
        # warm-only view told the user "0 tools" for a catalog their
        # own chat was actively offered. ``connected`` stays
        # transport-truthful; the catalog counts must not.
        idle = [e for e in scoped if e.session is None and self._entry_has_catalog(e)]
        # rep is the first warm entry (insertion order): the requester's own pool
        # when scoped, or a representative catalog for the aggregate operator
        # view — falling back to a cooled catalog when nothing is warm.
        rep = warm[0] if warm else (idle[0] if idle else None)
        cb_deadline = self._circuit_open_until.get(name)
        cb_open = cb_deadline is not None and time.monotonic() < cb_deadline
        last_refresh = self._last_refresh.get(name)
        return {
            "connected": bool(warm),
            "tools": len(rep.tools) if rep is not None and rep.tools else 0,
            "resources": len(rep.resources) if rep is not None and rep.resources else 0,
            "prompts": len(rep.prompts) if rep is not None and rep.prompts else 0,
            "error": self._last_error.get(name, ""),
            "transport": "streamable-http",
            "command": "",
            "url": "",
            "circuit_open": cb_open,
            "consecutive_failures": self._consecutive_failures.get(name, 0),
            "auth_type": self.server_auth_type(name) or "oauth_user",
            "user_pools": len(warm),
            "user_pools_idle": len(idle),
            "last_refresh_at": last_refresh[0] if last_refresh is not None else None,
            "last_refresh_outcome": last_refresh[1] if last_refresh is not None else None,
        }

    def get_all_server_status(
        self, user_id: str | None = None, *, aggregate: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Return live status for all configured servers.

        Includes ``oauth_user`` servers (which are absent from
        ``_server_configs``) so the console list reports their real per-user
        pool status instead of falling back to a DB-only "connecting" default.
        Their status is scoped to *user_id*, or aggregated across users when
        ``aggregate=True`` (see :meth:`get_server_status`).
        """
        result: dict[str, dict[str, Any]] = {}
        for name in list(self._server_configs):
            result[name] = self.get_server_status(name, user_id, aggregate=aggregate)
        for name in list(self._pool_server_names):
            if name not in result:
                result[name] = self.get_server_status(name, user_id, aggregate=aggregate)
        return result

    def reconcile_sync(self, storage: Any, timeout: int = 30) -> dict[str, Any]:
        """Reconcile DB-managed servers against DB state.

        Reads enabled ``mcp_servers`` rows from *storage*, then:
        - Connects servers in DB but not currently running.
        - Disconnects DB-managed servers no longer in DB (or disabled).
        - Reconnects DB-managed servers whose config has changed.

        Config-file servers (loaded at startup, not in ``_db_managed``)
        are never touched — only servers previously added via DB are
        eligible for removal.

        Returns ``{"added": [...], "removed": [...], "updated": [...]}``.
        """
        try:
            rows = storage.list_mcp_servers(enabled_only=True)
        except Exception:
            log.warning("reconcile_sync: failed to read mcp_servers table", exc_info=True)
            return {"added": [], "removed": [], "updated": []}

        # Prior (name -> pool auth_type) view, reconstructed from the tracked
        # pool-name sets. Diffing auth_type — not just names — below catches a
        # server MIGRATED in place between the two pool auth types
        # (oauth_user <-> oauth_obo, the flip the OBO feature enables): a
        # name-only diff sees the same name on both sides and misses it.
        # Mirrors the explicit two-type split of the set rebuilds just below.
        prev_pool_auth = {n: "oauth_user" for n in self._oauth_user_server_names}
        prev_pool_auth.update(dict.fromkeys(self._obo_server_names, "oauth_obo"))

        # Refresh the in-memory oauth_user name cache from the rows we
        # just read — feeds :meth:`server_auth_type` so callers (e.g.
        # ``web_search.resolve_web_search_client``) avoid a per-turn SQL
        # roundtrip.
        # Build both sets BEFORE storing so the two attribute stores are
        # adjacent: cross-thread readers (the eviction tick's
        # _retain_cooled) see at most a single-bytecode tear window on a
        # flip instead of a full row iteration between the swaps.
        new_oauth_user_names = {row["name"] for row in rows if row.get("auth_type") == "oauth_user"}
        new_obo_names = {row["name"] for row in rows if row.get("auth_type") == "oauth_obo"}
        self._oauth_user_server_names = new_oauth_user_names
        self._obo_server_names = new_obo_names
        new_pool_auth = {n: "oauth_user" for n in self._oauth_user_server_names}
        new_pool_auth.update(dict.fromkeys(self._obo_server_names, "oauth_obo"))
        # Pool servers newly registered OR migrated between pool auth types
        # since active sessions last primed.
        newly_added_pool = {
            name for name, at in new_pool_auth.items() if prev_pool_auth.get(name) != at
        }

        desired = _db_servers_to_config(rows)
        desired_names = set(desired)

        added: list[str] = []
        removed: list[str] = []
        updated: list[str] = []

        # Remove DB-managed servers no longer in DB (or disabled).
        # Config-file servers (not in _db_managed) are left untouched.
        for name in list(self._db_managed - desired_names):
            self.remove_server_sync(name, timeout=timeout)
            self._db_managed.discard(name)
            removed.append(name)

        # Add servers in DB but not running
        for name in desired_names - set(self._server_configs):
            result = self.add_server_sync(name, desired[name], timeout=timeout)
            if result.get("connected"):
                added.append(name)
                self._db_managed.add(name)
            else:
                log.warning("reconcile_sync: failed to add '%s': %s", name, result.get("error", ""))

        # Update DB-managed servers whose config has changed (cycle: remove + add).
        # Config-file servers with the same name as a DB server are left untouched.
        for name in desired_names & set(self._server_configs):
            if name not in self._db_managed:
                continue  # config-file server — DB doesn't own it
            if desired[name] != self._server_configs.get(name):
                log.info("Config changed for MCP server '%s', reconnecting", name)
                self.remove_server_sync(name, timeout=timeout)
                result = self.add_server_sync(name, desired[name], timeout=timeout)
                if result.get("connected"):
                    updated.append(name)
                    self._db_managed.add(name)
                else:
                    self._db_managed.discard(name)
                    log.warning(
                        "reconcile_sync: failed to reconnect '%s': %s",
                        name,
                        result.get("error", ""),
                    )

        if added or removed or updated:
            log.info(
                "MCP reconcile: +%d added, -%d removed, ~%d updated",
                len(added),
                len(removed),
                len(updated),
            )

        # Self-heal: a pool-backed (oauth_user / oauth_obo) server registered
        # while a user's ChatSession was already open would otherwise never
        # reach that session — ``prime_user_pools`` runs once at session start,
        # and for oauth_obo priming is the ONLY path tools take into the
        # catalog (there is no consent flow to warm it later). Re-prime every
        # active session's user so a mid-session registration surfaces its
        # tools automatically — no reconnect or fresh workstream needed.
        if newly_added_pool:
            self._reprime_active_users(len(newly_added_pool))

        return {"added": added, "removed": removed, "updated": updated}

    def _reprime_active_users(self, changed_count: int) -> None:
        """Schedule a pool re-warm for every active session's user after
        *changed_count* pool-backed servers were newly registered or migrated
        between pool auth types this reconcile (see :meth:`reconcile_sync`).

        Active users come from the tool-listener registry (each open ChatSession
        registers ``(user_id, callback)`` via :meth:`add_listener`); the
        ``user_id=None`` global/admin listener is skipped. ``prime_user_pools``
        is fire-and-forget — it SCHEDULES the mint/connect onto the mcp-loop and
        returns, no-opping for an already-warm pool, a user without a captured
        credential, or a down loop — so the log below reports what was
        SCHEDULED, not what completed. The call is guarded per-user (mirroring
        the session.py call sites) so one user's scheduling failure can't abort
        the loop or 500 the reload endpoint.

        Fan-out note: only the infrequent, operator-driven reload path reaches
        here and each user's mint concurrency is already capped
        (``_PRIME_MAX_CONCURRENCY``); a shared cross-user mint cap is left as a
        follow-up if IdP rate-limiting is ever observed under a large active-user
        count.
        """
        user_ids = self._live_listener_uids()
        for user_id in user_ids:
            try:
                self.prime_user_pools(user_id)
            except Exception:
                log.debug(
                    "mcp reconcile re-prime scheduling failed user=%s",
                    user_id,
                    exc_info=True,
                )
        if user_ids:
            log.info(
                "MCP reconcile: scheduled pool re-prime for %d active session "
                "user(s) after %d pool server change(s)",
                len(user_ids),
                changed_count,
            )

    # -- query methods -------------------------------------------------------

    def get_tools(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """Return MCP tools in OpenAI function-calling format.

        ``user_id=None`` returns the global static-path catalog only —
        the legacy behaviour every pre-Phase-7 caller relies on. A
        string ``user_id`` returns the merged view: static catalog
        followed by that user's cached pool tools, read from
        ``_user_tools`` (a single dict-get, atomic under GIL). The
        list snapshot is materialised on the mcp-loop by
        :meth:`_rebuild_user_tool_map` and replaced atomically — sync
        threads never iterate ``_user_pool_entries`` directly, so the
        mcp-loop is free to insert/pop entries concurrently.
        Callers that don't carry a session-bound user (boot-time
        logging, web_search backend resolution) MUST use the default.

        Returned dicts are shallow-copied; nested objects are shared
        with the manager's catalog, mirroring the pre-Phase-7 contract.
        """
        base = [dict(t) for t in self._tools]
        if user_id is None:
            return base
        base.extend(dict(t) for t in self._user_tools.get(user_id, []))
        return base

    def get_resources(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """Return discovered MCP resources (shallow-copied dicts).

        ``user_id=None`` returns the global static-path catalog only —
        the legacy behaviour every pre-Phase-7b caller relies on. A
        string ``user_id`` returns the merged view: per-user pool
        resources first (per scope decision 0.1 — per-user-first
        ordering), then the static catalog. Reads ``_user_resources``
        via a single dict-get (atomic under GIL).

        Returned dicts are shallow-copied; nested objects are shared
        with the manager's catalog, mirroring the pre-Phase-7b contract.
        """
        if user_id is None:
            return [dict(r) for r in self._resources]
        merged: list[dict[str, Any]] = [dict(r) for r in self._user_resources.get(user_id, [])]
        merged.extend(dict(r) for r in self._resources)
        return merged

    def get_prompts(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """Return discovered MCP prompts (shallow-copied dicts).

        ``user_id=None`` returns the global static-path catalog only.
        A string ``user_id`` returns the merged view: per-user pool
        prompts first (scope decision 0.1), then static.
        """
        if user_id is None:
            return [dict(p) for p in self._prompts]
        merged: list[dict[str, Any]] = [dict(p) for p in self._user_prompts.get(user_id, [])]
        merged.extend(dict(p) for p in self._prompts)
        return merged

    @property
    def resource_count(self) -> int:
        """Number of discovered static resources (no allocation).

        Process-global by design — admin endpoints rely on this contract.
        Use :meth:`resource_count_for_user` for the per-user count that
        includes the user's pool resources.
        """
        return len(self._resources)

    @property
    def prompt_count(self) -> int:
        """Number of discovered static prompts (no allocation).

        Process-global by design — admin endpoints rely on this contract.
        Use :meth:`prompt_count_for_user` for the per-user count that
        includes the user's pool prompts.
        """
        return len(self._prompts)

    def resource_count_for_user(self, user_id: str | None = None) -> int:
        """Return ``len(static) + len(user_pool)`` resources.

        ``user_id=None`` returns the static-only count (matches the
        ``resource_count`` property). A string ``user_id`` adds that
        user's pool resources. Used by ChatSession's ``read_resource``
        tool gating so a pool-only user still sees the tool when
        their pool has resources but the static catalog is empty
        (scope decision 0.2).
        """
        base = len(self._resources)
        if user_id is None:
            return base
        return base + len(self._user_resources.get(user_id, []))

    def prompt_count_for_user(self, user_id: str | None = None) -> int:
        """Return ``len(static) + len(user_pool)`` prompts.

        See :meth:`resource_count_for_user` — same shape, prompts
        instead of resources.
        """
        base = len(self._prompts)
        if user_id is None:
            return base
        return base + len(self._user_prompts.get(user_id, []))

    def is_mcp_tool(self, func_name: str, *, user_id: str | None = None) -> bool:
        """Check whether *func_name* belongs to an MCP server.

        ``user_id=None`` (default) asks "is this a static-path tool?" —
        the answer is process-global. Boot-time and per-node callers
        (e.g. ``resolve_web_search_client``) MUST use the default
        because they don't carry a session-bound user identity.

        A string ``user_id`` extends the lookup to that user's pool
        catalog: returns ``True`` if ``func_name`` is in either the
        static map OR the user's per-user tool map. Pool tools become
        reachable via this gate ONLY once a session-bound caller
        threads its ``user_id`` through; CLI sessions default
        ``user_id=""`` and so cannot see pool tools — documented
        limitation.
        """
        if func_name in self._tool_map:
            return True
        if user_id is None:
            return False
        user_map = self._user_tool_map.get(user_id)
        return user_map is not None and func_name in user_map

    def is_mcp_prompt(self, name: str, *, user_id: str | None = None) -> bool:
        """Check whether *name* is a known MCP prompt.

        ``user_id=None`` (default) asks "is this a static-path prompt?" —
        the answer is process-global. A string ``user_id`` extends the
        lookup to that user's pool catalog: returns ``True`` if ``name``
        is in either the static map OR the user's per-user prompt map.
        Mirrors :meth:`is_mcp_tool` (scope decision 0.1, per-user-first
        is moot for prompts because their prefixed names are
        per-server-disjoint by construction).
        """
        if name in self._prompt_map:
            return True
        if user_id is None:
            return False
        user_map = self._user_prompt_map.get(user_id)
        return user_map is not None and name in user_map

    def server_auth_type(self, server_name: str) -> str | None:
        """Return the pool-backed auth type (``'oauth_user'`` / ``'oauth_obo'``), else ``None``.

        In-memory accessor for the per-turn callers that need to
        distinguish pool-backed servers from static-path ones without a
        SQL roundtrip. ``None`` means "either static-path or unknown" —
        the boot-time / per-node web_search resolver only uses this as
        a defence-in-depth gate (via :func:`is_user_scoped_auth`), so a
        missing-cache miss is safe (the outer ``is_mcp_tool`` check
        already proves the server is in ``_tool_map``, which by
        construction excludes pool-backed servers).
        Populated by ``reconcile_sync`` and ``create_mcp_client``.
        """
        if server_name in self._oauth_user_server_names:
            return "oauth_user"
        if server_name in self._obo_server_names:
            return "oauth_obo"
        return None

    def _is_pool_server(self, server_name: str) -> bool:
        """True when *server_name* uses per-user pool sessions (no static session).

        Derived from :meth:`server_auth_type` so pool membership has exactly
        one definition over the two per-auth-type registries.
        """
        return self.server_auth_type(server_name) is not None

    @property
    def _pool_server_names(self) -> set[str]:
        """Union of the per-auth-type pool registries (oauth_user + oauth_obo).

        The set-level counterpart to :meth:`_is_pool_server`, so the iteration
        sites (priming, keep-hot sweep, status) share ONE definition of "which
        servers are pool-backed" — a future third pool-backed auth type is added
        in one place instead of being missed at an ad-hoc inline union.
        """
        return self._oauth_user_server_names | self._obo_server_names

    @property
    def server_count(self) -> int:
        return sum(1 for s in self._static_servers.values() if s.session is not None)

    @property
    def error_count(self) -> int:
        """Number of servers currently in error state."""
        return len(self._last_error)

    @property
    def server_names(self) -> list[str]:
        """Return configured server names."""
        return list(self._server_configs.keys())

    # -- tool invocation -----------------------------------------------------

    def _cb_gate(self, server_name: str) -> None:
        """Check circuit breaker before dispatching to *server_name*.

        Raises ``RuntimeError`` if the circuit is open and cooldown has not
        expired.  When the cooldown has expired (half-open), clears the
        deadline so the probe attempt is allowed through.
        """
        is_open, cooldown_expired = self._cb_check(server_name)
        if is_open and not cooldown_expired:
            remaining = self._circuit_open_until.get(server_name, 0) - time.monotonic()
            raise RuntimeError(
                f"MCP server '{server_name}' circuit open "
                f"(cooldown {remaining:.0f}s remaining). "
                f"Use '/mcp refresh {server_name}' to retry manually."
            )
        if cooldown_expired:
            # Remove deadline so concurrent callers aren't rejected while the
            # probe is in-flight.  This intentionally allows multiple callers
            # through rather than a single probe: reconnects serialize on the
            # event loop via _connect_one's guard, and if the server is truly
            # broken the first failure re-trips the circuit immediately.
            self._circuit_open_until.pop(server_name, None)

    def _spawn_background(self, coro: Coroutine[Any, Any, Any], label: str) -> asyncio.Task[Any]:
        """Schedule *coro* as a tracked background task (loop thread only).

        Holds a strong reference until completion and retrieves the task's
        outcome in a done-callback: failures are logged once, here, at
        warning — never deferred to garbage collection, where they surface
        as "Task exception was never retrieved" on whatever stream happens
        to be attached at the time.  ``shutdown()`` cancels anything still
        tracked before stopping the loop.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            try:
                if not t.cancelled():
                    exc = t.exception()
                    if exc is not None:
                        log.warning("MCP background %s failed", label, exc_info=exc)
            finally:
                # Discard LAST so set-emptiness means "done AND reported" —
                # a watcher keying on emptiness must never race the warning.
                self._background_tasks.discard(t)

        task.add_done_callback(_done)
        return task

    # -- static-server health loop (autonomous reconnect + liveness) ----------

    async def _static_health_loop(self) -> None:
        """Long-running coroutine — keeps static MCP servers connected.

        The autonomous reconnect Turnstone otherwise lacks. Every other reconnect
        path is lazy (a tool dispatch, an operator refresh, a config edit), and
        the SDK's own reconnect is a bounded 2-attempt burst on the
        streamable-http GET stream ONLY (verified: mcp 1.28.1) — no backoff,
        nothing for the other transports. So a static server that goes down and
        comes back while nobody is dispatching to it stays dead until someone
        acts. Each tick, on the mcp-loop:

          * a DISCONNECTED server (``session is None``) is reconnected on a
            capped, jittered, forever backoff (:meth:`_static_reconnect_delay`);
          * a CONNECTED server is liveness-pinged (:meth:`_static_ping_one`) and a
            dead-but-idle one — which the SDK leaves as a non-None session with
            closed streams, so nothing else notices until a dispatch fails — is
            evicted so the next tick reconnects it.

        Backoff is the health loop's OWN clock; the circuit breaker stays the
        DISPATCH fail-fast gate (a tool call to a down server errors immediately
        rather than blocking on the forever-retry). The loop keeps breaker state
        in sync so an open breaker closes once it reconnects. Static-only:
        ``oauth_user`` pools are managed by their own paths.

        Cancellation policy: only a GENUINE shutdown (``shutdown()`` cancelling
        this task) stops the loop. A stray per-server ``CancelledError`` that
        escapes the tick (a wedged anyio transport surfacing a cancel-scope
        poison) must not silently kill the only autonomous reconnect driver —
        ``Task.cancelling()`` is the discriminator, because a real cancel
        request bumps the task's pending-cancel count before the
        ``CancelledError`` is delivered.
        """
        while True:
            try:
                sleep_s = await self._static_health_tick()
                await asyncio.sleep(sleep_s)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is None or task.cancelling():
                    return  # genuine shutdown
                log.warning("MCP static health: stray cancellation absorbed; continuing")
                await asyncio.sleep(self._static_health_check_s)
            except (Exception, BaseExceptionGroup):
                # BaseExceptionGroup explicitly: an anyio task-group collapse
                # that wraps a stray CancelledError is BaseException-derived
                # and would otherwise kill the only autonomous reconnect
                # driver, silently. A genuine shutdown racing the group is
                # still honored: the pending cancel fires at the sleep below
                # and the CancelledError arm above returns.
                log.warning("MCP static health iteration failed", exc_info=True)
                await asyncio.sleep(self._static_health_check_s)

    async def _static_health_tick(self) -> float:
        """One health pass. Returns seconds to sleep until the next due event.

        MUST run on the mcp-loop. Servers are processed CONCURRENTLY (one slow
        connect must not block liveness for every other server) with per-server
        exception isolation. The returned sleep is against a FRESHLY-read clock —
        the per-server work can take seconds, so the tick-start ``now`` is stale
        by return — computed from the soonest per-server monotonic deadline and
        clamped at 0.5s minimum to avoid busy-spinning (a deadline shorter
        than 0.5s adds at most 0.5s latency before the next tick re-checks).
        """
        now = time.monotonic()
        names = [
            name
            for name in list(self._server_configs.keys())
            # Per-user pools are managed separately; ``__`` names can never
            # connect (``_connect_one``'s reserved-delimiter guard), so retrying
            # them forever would only spam ``log.error`` every interval.
            if not self._is_pool_server(name) and "__" not in name
        ]
        if not names:
            return self._static_health_check_s
        results = await asyncio.gather(
            *(self._static_health_one(name, now) for name in names),
            return_exceptions=True,
        )
        # FRESH clock for every deadline computed after the gather — the
        # per-server work above can burn tens of seconds, so ``now`` is stale.
        after = time.monotonic()
        soonest = after + self._static_health_check_s
        for name, res in zip(names, results, strict=True):
            if isinstance(res, BaseException):
                # Per-server exception isolation — INCLUDING CancelledError. A
                # genuine shutdown cancels the ``await gather`` itself, which
                # raises CancelledError at the await directly (an outer cancel
                # never lands in the results list, ``return_exceptions``
                # notwithstanding) and propagates to ``_static_health_loop``'s
                # shutdown path. A CancelledError IN the results is per-server
                # fallout (a stray anyio cancel-scope poison from a wedged
                # transport) — re-raising it here killed the whole loop.
                log.debug(
                    "MCP static health: server '%s' pass failed",
                    name,
                    exc_info=(type(res), res, res.__traceback__),
                )
                due = after + self._static_health_check_s
            else:
                due = res
            soonest = min(soonest, due)
        # Fresh clock (fix): deadlines are absolute-monotonic; sleeping
        # ``soonest - now`` would over-sleep by the tick's own elapsed time.
        return max(0.5, soonest - time.monotonic())

    async def _static_health_one(self, name: str, now: float) -> float:
        """Ping a connected server or reconnect a disconnected one; return its
        next-due monotonic deadline. Runs concurrently per server in the tick."""
        state = self._static_servers.get(name)
        if state is not None and state.session is not None:
            return await self._static_ping_one(name, now)
        return await self._static_reconnect_one(name)

    def _static_reconnect_delay(self, attempt: int) -> float:
        """Full-jitter capped-exponential backoff: ``uniform(0, min(CAP, BASE*2^n))``.

        Retry forever — ``attempt`` is unbounded; :meth:`_capped_exponential`
        clamps the exponent before ``2**n`` explodes, so past the cap every
        attempt draws from the same ``[0, CAP]`` window.
        """
        ceiling = self._capped_exponential(
            self._STATIC_RECONNECT_BASE_S, self._STATIC_RECONNECT_MAX_S, attempt
        )
        return random.uniform(0.0, ceiling)

    async def _static_reconnect_one(self, name: str) -> float:
        """Reconnect a disconnected static server if its backoff has elapsed.

        Returns the monotonic deadline of the next attempt so the loop can
        sleep until then. Routes through :meth:`_ensure_static_connected` — if
        another driver is mid-connect we queue briefly on the per-name lock and
        then REUSE its session instead of piling a redundant rebuild on the
        shared state; the primitive also owns the attempt bound and the breaker
        (a failure here only advances the health loop's OWN backoff clock).

        Uses a FRESH monotonic clock internally (not a caller-passed tick-start
        snapshot that could be stale after a slow sibling ``gather``).
        """
        fresh_now = time.monotonic()
        due = self._static_reconnect_next.get(name, fresh_now)
        if fresh_now < due:
            return due
        cfg = self._server_configs.get(name)
        if not cfg:
            return time.monotonic() + self._static_health_check_s
        try:
            log.info("MCP static health: reconnecting '%s'", name)
            session = await self._ensure_static_connected(name, cfg)
        except (Exception, BaseExceptionGroup) as exc:
            # The primitive already recorded the breaker failure and dropped
            # any partially-initialized session; only the health loop's own
            # backoff state advances here. BaseExceptionGroup explicitly: a
            # transport task-group failure must advance the backoff like any
            # other connect error, not skip it (a hot every-tick retry) by
            # escaping to the tick's exception isolation.
            attempt = self._static_reconnect_attempt.get(name, 0) + 1
            self._static_reconnect_attempt[name] = attempt
            delay = self._static_reconnect_delay(attempt)
            next_due = time.monotonic() + delay
            self._static_reconnect_next[name] = next_due
            # Loud on the first few failures, then decays to debug so a long /
            # permanent outage doesn't spam the log on every forever-retry.
            log_fn = log.warning if attempt <= self._CB_FAILURE_THRESHOLD else log.debug
            log_fn(
                "MCP static health: reconnect '%s' failed (attempt %d), next in %.0fs: %s",
                name,
                attempt,
                delay,
                exc,
            )
            return next_due
        if session is None:
            # Deliberate skip — removed from config mid-flight, or a sibling
            # dispatch is still in flight on the old (evicted) stack. Not a
            # failure: no backoff bump, no breaker; re-check shortly.
            return time.monotonic() + 1.0
        # Success — reset the health-loop's own backoff, schedule the first ping,
        # and reconcile catalog drift off the critical path (mirrors
        # ``_cb_auto_reconnect``: the session is usable immediately). Breaker
        # state was already settled by ``_ensure_static_connected``: only the
        # open-circuit deadline cleared, the failure count kept so a
        # connect-OK / calls-FAIL server still escalates to a trip — a real
        # dispatch SUCCESS is what resets it.
        self._static_reconnect_attempt.pop(name, None)
        self._static_reconnect_next.pop(name, None)
        next_ping = time.monotonic() + self._static_health_check_s
        self._static_next_ping[name] = next_ping
        self._spawn_background(
            self._refresh_server_logged(name),
            f"catalog refresh after static health reconnect '{name}'",
        )
        log.info("MCP static health: reconnected '%s'", name)
        return next_ping

    def _schedule_next_ping(self, name: str) -> float:
        """Set and return the next liveness-ping deadline (fresh monotonic clock)."""
        next_ping = time.monotonic() + self._static_health_check_s
        self._static_next_ping[name] = next_ping
        return next_ping

    async def _static_ping_one(self, name: str, now: float) -> float:
        """Liveness-ping a connected static server; evict a dead-but-idle one.

        Returns the monotonic deadline of the next ping. Classification mirrors
        the dispatch path's ``_record_and_evict_on_dead_transport``:

          * a genuinely DEAD transport (``_is_dead_transport``) is evicted
            (``session = None``) so the next tick reconnects it, and its breaker
            records a failure so dispatch fails fast meanwhile;
          * a protocol ``McpError``, an ``httpx.PoolTimeout``, or a plain ping
            TIMEOUT is "slow, not dead" — the ping is rescheduled WITHOUT evicting
            or tripping the breaker (a strict 5s ping vs a 120s dispatch would
            otherwise churn a heavy-but-working server every cycle);
          * a BUSY server (``in_flight > 0``) is skipped entirely — it can't
            answer a ping mid-call, and tearing it down would abort that call
            (the ``StaticServerState`` in-flight interlock, mirroring the pool
            path's ``_close_pool_entry_if_idle``).

        Clock discipline: the tick-start *now* may gate "is it due?", but every
        WRITTEN/RETURNED deadline uses a fresh ``time.monotonic()`` — after a
        slow sibling op in the same gather a stale base lands the next ping in
        the past and collapses the cadence into an every-tick re-ping.
        """
        # Recovered-server hygiene: a live session means any stale health-loop
        # backoff (left by a dispatch/operator reconnect the loop didn't drive)
        # is wrong — clear it so a later autonomous reconnect isn't delayed by a
        # stale ``due``.
        self._static_reconnect_attempt.pop(name, None)
        self._static_reconnect_next.pop(name, None)

        due = self._static_next_ping.get(name, now)
        if now < due:
            return due
        state = self._static_servers.get(name)
        session = state.session if state is not None else None
        if session is None:
            # Raced an eviction — due now; the reconnect path handles it next tick.
            return time.monotonic()
        if state is not None and state.in_flight > 0:
            # Busy serving a dispatch → demonstrably alive; skip ping + evict.
            return self._schedule_next_ping(name)
        try:
            # invariant-18: ``asyncio.timeout`` (NOT ``wait_for``) so ``send_ping``
            # runs in THIS task and anyio cancel-scope exit stays same-task.
            async with asyncio.timeout(self._STATIC_HEALTH_PING_TIMEOUT_S) as ping_timeout:
                await session.send_ping()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as exc:
            # ``asyncio.timeout`` converts its OWN expiry to ``TimeoutError``;
            # ``ping_timeout.expired()`` is the unambiguous signal. A wedged anyio
            # transport can surface the timeout-scoped cancel as a stray
            # CancelledError / BaseExceptionGroup, so we catch the full trio — but
            # a bare CancelledError when the timeout did NOT fire is an EXTERNAL
            # cancel (loop shutdown) and MUST propagate so ``_static_health_loop``
            # stops via its ``except asyncio.CancelledError: return``.
            if not ping_timeout.expired() and isinstance(exc, asyncio.CancelledError):
                raise
            if ping_timeout.expired():
                dead = False
            elif isinstance(exc, BaseExceptionGroup):
                dead = any(_is_dead_transport(e) for e in exc.exceptions)
            else:
                dead = _is_dead_transport(exc)
            # Re-check in-flight under this synchronous handler: a dispatch may
            # have started during the ping window; never evict a busy server.
            # Session-identity: only act on the session we actually PINGED — a
            # concurrent reconnect may have installed a fresh one during the await
            # window, and our stale ping's death says nothing about it (evicting
            # or tripping the breaker for it would undo a good reconnect).
            evict = self._static_servers.get(name)
            busy = evict is not None and evict.in_flight > 0
            if dead and evict is not None and not busy and evict.session is session:
                self._cb_record_failure(name)
                self._drop_static_session_and_stamp(name, evict)
                asap = time.monotonic()
                self._static_reconnect_next[name] = asap  # reconnect asap
                self._static_reconnect_attempt.pop(name, None)
                log.info(
                    "MCP static health: '%s' failed liveness ping (%s); evicting to reconnect",
                    name,
                    type(exc).__name__,
                )
                return asap
            # Slow / protocol / pool-saturation / busy / swapped-session: not an
            # actionable death — reschedule, don't evict, don't trip the breaker.
            log.debug(
                "MCP static health: '%s' ping did not confirm liveness (%s); rescheduling",
                name,
                type(exc).__name__,
            )
            return self._schedule_next_ping(name)
        return self._schedule_next_ping(name)

    def _cb_auto_reconnect(self, server_name: str) -> Any:
        """Attempt reconnection for a disconnected server during half-open probe.

        Returns the new (or concurrently re-established) session on success, or
        raises on failure. Routes through :meth:`_ensure_static_connected`, so
        the per-name lock, live-session reuse, config re-check, and the
        ``in_flight`` deferral are shared with the health loop and
        ``_refresh_all`` — a dispatch can never tear down, resurrect, or
        blindly re-do a reconnect another driver already made.

        Breaker ownership: connect outcomes are recorded INSIDE the primitive
        only. The sync-boundary ``result(timeout=...)`` below deliberately
        records NOTHING — its expiry usually means the per-name lock was merely
        contended (a concurrent health-loop attempt is bounded at
        ``_STATIC_RECONNECT_ATTEMPT_TIMEOUT_S``, above this wait), and counting
        lock-wait as a server failure trips the breaker for a reachable server.
        A cancelled attempt proves nothing; the next real connect outcome is
        recorded where it is observed.
        """
        cfg = self._server_configs.get(server_name)
        if not cfg or self._loop is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected")

        async def _reconnect_for_dispatch() -> Any:
            # defer_if_busy=False: a dispatch NEEDS the session now and may tear
            # down a sibling call rather than hard-fail a reachable server (the
            # in_flight defer is for autonomous drivers that retry on their own).
            return await self._ensure_static_connected(server_name, cfg, defer_if_busy=False)

        # A fresh coroutine/task per attempt: a timed-out attempt is cancelled
        # below, and the next dispatch starts clean instead of re-entering a
        # half-cancelled anyio scope.
        reconnect_future = asyncio.run_coroutine_threadsafe(_reconnect_for_dispatch(), self._loop)
        try:
            session = reconnect_future.result(timeout=self._STATIC_RECONNECT_CALLER_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            reconnect_future.cancel()
            # No breaker record: see docstring — lock contention is not a
            # server failure, and this boundary cannot tell the two apart.
            raise RuntimeError(f"MCP server '{server_name}' reconnect timed out") from None
        except (Exception, BaseExceptionGroup) as exc:
            # Real connect failures were already recorded by the primitive;
            # recording here again would double-count one outcome.
            # BaseExceptionGroup: normalize a transport task-group failure to
            # the same RuntimeError shape every dispatch caller expects.
            raise RuntimeError(f"MCP server '{server_name}' reconnect failed: {exc}") from None
        if session is None:
            # Deliberate skip: the server was removed while we queued, or a
            # sibling call is still in flight on the old (evicted) stack. Fail
            # this dispatch cleanly; not a breaker-worthy server failure.
            raise RuntimeError(f"MCP server '{server_name}' is unavailable (removed or busy)")

        # Schedule catalog refresh on the loop without blocking the caller.
        # The reconnected session is valid for the imminent dispatch; catalog
        # drift will be reconciled on the loop in the background.  The task is
        # tracked; a refresh FAILURE is logged inside ``_refresh_server_logged``
        # (type + message, never the done-callback's ``exc_info`` — the chain
        # carries the configured bearer) — this except only covers the
        # scheduling itself.
        def _schedule_refresh() -> None:
            try:
                self._spawn_background(
                    self._refresh_server_logged(server_name),
                    f"catalog refresh after reconnect for '{server_name}'",
                )
            except Exception:
                log.warning(
                    "Scheduling catalog refresh after reconnect failed for '%s'",
                    server_name,
                    exc_info=True,
                )

        self._loop.call_soon_threadsafe(_schedule_refresh)
        return session

    def _record_and_evict_on_dead_transport(self, server_name: str, exc: BaseException) -> None:
        """Shared static-dispatch failure handling for ``call_tool_sync`` /
        ``read_resource_sync`` / ``get_prompt_sync`` (call from their ``except``,
        then re-raise).

        Protocol errors (``McpError`` from a healthy connection that rejected the
        request) do NOT trip the breaker. A dead transport — anyio
        Closed/BrokenResourceError, the SDK-swallowed ``McpError(CONNECTION_CLOSED)``,
        a server-restarted session, or a gone httpx connection — IS a transport
        failure even when it is an ``McpError``, so it trips the breaker AND evicts
        the session (leaving the owner/streams for ``_connect_one_locked``'s
        stale-guard close protocol to reap). Eviction is what lets the next
        dispatch's ``session is None`` check fire ``_cb_auto_reconnect`` instead
        of re-using the corpse.
        """
        dead = _is_dead_transport(exc)
        if dead or not isinstance(exc, McpError):
            self._cb_record_failure(server_name)
        if dead:
            evict = self._static_servers.get(server_name)
            if evict is not None:
                self._drop_static_session_and_stamp(server_name, evict)

    async def _static_session_op(self, server_name: str, op: Coroutine[Any, Any, Any]) -> Any:
        """Await a static session op on the mcp-loop, pinned against eviction.

        Increments the server's ``in_flight`` counter for the duration of the
        awaited op and decrements it in ``finally`` (mirrors the pool path's
        ``entry.in_flight`` accounting at ``_dispatch_pool_with_entry``). The
        health-loop ping skips and never evicts a server with ``in_flight > 0``,
        so a long-running ``call_tool`` can't be torn down mid-flight by a
        liveness reconnect. MUST be scheduled onto the mcp-loop; the
        increment/decrement wraps ONLY the session op, not the sync bridge.
        ``StaticServerState`` identity is stable across reconnects (PR #296
        invariant 5: ``_ensure_static_state`` get-or-creates), so the same
        object is decremented that was incremented.
        """
        state = self._static_servers.get(server_name)
        if state is None:
            return await op
        state.in_flight += 1
        try:
            return await op
        finally:
            state.in_flight -= 1

    def call_tool_sync(
        self,
        func_name: str,
        arguments: dict[str, Any],
        *,
        user_id: str | None = None,
        timeout: int = 120,
        is_interactive_for_consent: bool = True,
    ) -> str:
        """Execute an MCP tool call synchronously (blocks the calling thread).

        Dispatches an async ``tools/call`` to the background event loop and
        waits for the result.  Includes circuit-breaker gating and automatic
        reconnection for servers recovering from failure.

        When ``user_id`` is supplied AND the resolved server's ``auth_type``
        is ``oauth_user``, dispatch goes through the per-(user, server)
        pool. Otherwise the call takes the byte-identical static path.

        Pool-path 401/403 handling: the SDK's ``post_writer`` swallows
        ``httpx.HTTPStatusError``; we recover the upstream auth signal
        via a response-hook carrier owned by the pool entry (bound to
        the entry's persistent ``httpx.AsyncClient`` at first connect;
        see ``PoolEntryState.auth_capture``). A 401 triggers one
        refresh-and-retry with ``force_refresh=True``; persistent 401 emits
        ``mcp_consent_required``. A 403 with
        ``WWW-Authenticate: error="insufficient_scope"`` emits
        ``mcp_insufficient_scope`` with the parsed scope set. Other
        403s emit ``mcp_tool_call_forbidden``. Auth failures NEVER
        trip the per-server breaker.
        """
        mapping = self._tool_map.get(func_name)
        server_name: str | None = None
        original_name: str | None = None
        if mapping is not None:
            server_name, original_name = mapping

        # Pool dispatch is gated on (a) caller passing user_id and
        # (b) the server row being auth_type=oauth_user. The current
        # tool-map only carries static-path entries; pool catalogs are
        # not merged in. Consequently, a pool dispatch reaches this
        # branch only when the caller supplied func_name as
        # ``mcp__{server}__{tool}`` and we resolve auth_type via storage.
        if user_id and self._app_state is not None and self._storage is not None:
            pool_target = self._resolve_pool_target(func_name, server_name, original_name)
            if pool_target is not None:
                return self._dispatch_pool_sync(
                    user_id=user_id,
                    server_name=pool_target[0],
                    original_name=pool_target[1],
                    arguments=arguments,
                    server_row=pool_target[2],
                    timeout=timeout,
                    is_interactive_for_consent=is_interactive_for_consent,
                )

        if mapping is None or server_name is None or original_name is None:
            raise ValueError(f"Unknown MCP tool: {func_name}")

        self._cb_gate(server_name)

        state = self._static_servers.get(server_name)
        session = state.session if state is not None else None
        if session is None:
            session = self._cb_auto_reconnect(server_name)
        assert self._loop is not None

        future = asyncio.run_coroutine_threadsafe(
            self._static_session_op(server_name, session.call_tool(original_name, arguments)),
            self._loop,
        )
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._cb_record_failure(server_name)
            raise TimeoutError(f"MCP tool call timed out after {timeout}s") from None
        except Exception as exc:
            self._record_and_evict_on_dead_transport(server_name, exc)
            raise

        self._cb_record_success(server_name)
        return _decode_tool_result(result)

    # -- pool dispatch -------------------------------------------------------

    def _resolve_pool_target(
        self,
        func_name: str,
        static_server: str | None,
        static_original: str | None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Resolve ``(server_name, original_name, server_row)`` for pool dispatch.

        Returns ``None`` when the call should fall through to the static
        path. Pool catalogs do not contribute to ``_tool_map`` today,
        so a pool tool is invoked by passing the prefixed name
        ``mcp__{server}__{tool}`` directly. ``mcp_servers.auth_type``
        confirms pool eligibility.

        Presence in ``_tool_map`` (signalled by non-None ``static_server``
        / ``static_original`` from the caller's lookup) is sufficient
        to short-circuit without a DB hop: ``_db_servers_to_config``
        strips ``oauth_user`` rows on the way into ``_static_servers``,
        so any name that landed in ``_tool_map`` is by construction
        static-path. The resolved row is threaded back to the caller so
        ``_dispatch_pool`` avoids a duplicate ``get_mcp_server_by_name``
        round-trip.
        """
        if static_server is not None and static_original is not None:
            return None
        if not func_name.startswith("mcp__") or func_name.count("__") < 2:
            return None
        # Parse ``mcp__{server}__{rest}`` — server may itself be empty
        # (rejected during admin), original may contain ``__``.
        _, server_name, original = func_name.split("__", 2)
        if not server_name or not original:
            return None
        row = self._lookup_server_row(server_name)
        if row is None or not is_user_scoped_auth(row.get("auth_type")):
            return None
        return server_name, original, row

    async def _pool_token_lookup(
        self,
        server_row: dict[str, Any],
        user_id: str,
        server_name: str,
        *,
        force_refresh: bool,
    ) -> TokenLookupResult:
        """Classified token lookup routed by the server's auth model.

        ``oauth_obo`` servers mint from the user's single captured
        credential (:func:`get_obo_access_token_classified`); everything
        else keeps the per-(user, server) refresh-grant path. Both share
        the ``TokenLookupResult`` vocabulary, so the dispatcher's error
        mapping below is auth-model-agnostic.
        """
        if str(server_row.get("auth_type") or "") == "oauth_obo":
            return await get_obo_access_token_classified(
                app_state=self._app_state,
                user_id=user_id,
                server_name=server_name,
                force_refresh=force_refresh,
                server_row=server_row,
            )
        return await get_user_access_token_classified(
            app_state=self._app_state,
            user_id=user_id,
            server_name=server_name,
            force_refresh=force_refresh,
        )

    def _lookup_server_row(self, server_name: str) -> dict[str, Any] | None:
        """Return the ``mcp_servers`` row for *server_name*, or None."""
        if self._storage is None:
            return None
        try:
            row: dict[str, Any] | None = self._storage.get_mcp_server_by_name(server_name)
        except Exception:
            log.debug("mcp_pool.server_lookup_failed", exc_info=True)
            return None
        return row

    def _resolve_pool_target_resource(
        self, uri: str, user_id: str
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Resolve ``(server_name, uri, server_row)`` for pool resource reads.

        Per scope decision 0.1: per-user-first resolution. Consults the
        user's ``_user_resource_map`` first, then the user's
        ``_user_template_prefixes`` (longest-prefix match). Returns
        ``None`` when the URI doesn't match a pool entry — the caller
        falls through to the static path.

        Pool eligibility is gated on ``server_row.auth_type ==
        'oauth_user'``: a per-user map entry that points at a static-
        path server is treated as a miss (the static path will pick it
        up). Defence-in-depth — _user_resource_map is populated only
        from pool entries, so this case shouldn't occur in practice.
        """
        user_map = self._user_resource_map.get(user_id) or {}
        mapping = user_map.get(uri)
        if mapping is None:
            # Try per-user templates (longest-prefix wins within scope).
            best: tuple[str, str] | None = None
            best_len = 0
            for prefix, prefix_mapping in (self._user_template_prefixes.get(user_id) or {}).items():
                if uri.startswith(prefix) and len(prefix) > best_len:
                    best = prefix_mapping
                    best_len = len(prefix)
            if best is None:
                return None
            server_name = best[0]
        else:
            server_name = mapping[0]
        row = self._lookup_server_row(server_name)
        if row is None or not is_user_scoped_auth(row.get("auth_type")):
            return None
        return server_name, uri, row

    def _resolve_pool_target_prompt(
        self,
        prefixed_name: str,
        static_server: str | None,
        static_original: str | None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Resolve ``(server_name, original_name, server_row)`` for pool prompts.

        Mirror of :meth:`_resolve_pool_target` for prompts: prompts
        carry the ``mcp__{server}__{name}`` prefix shape so the same
        parsing applies. Presence in static ``_prompt_map`` (signalled
        by non-None ``static_server`` / ``static_original``) short-
        circuits to None — the static path owns that name.
        """
        if static_server is not None and static_original is not None:
            return None
        if not prefixed_name.startswith("mcp__") or prefixed_name.count("__") < 2:
            return None
        _, server_name, original = prefixed_name.split("__", 2)
        if not server_name or not original:
            return None
        row = self._lookup_server_row(server_name)
        if row is None or not is_user_scoped_auth(row.get("auth_type")):
            return None
        return server_name, original, row

    def _record_pending_consent_best_effort(
        self,
        *,
        user_id: str,
        server_name: str,
        result: str,
    ) -> None:
        """Persist a deferred-consent row for non-interactive callers.

        Called from the three sync dispatchers when the dispatch returns
        a structured-error envelope AND the caller is not interactive
        (CHAT / SCHEDULED).  Filters out structured-error codes that
        aren't user-consent-shaped (key-unknown, url-insecure,
        *_forbidden) — those are operator-actionable and outside the
        scope of the dashboard pending-consent badge.

        Best-effort.  A storage exception is logged but never raised:
        the structured-error envelope must reach the agent unchanged
        regardless of whether the pending-consent row was persisted, so
        a transient DB failure doesn't change the agent-observable
        contract.
        """
        if self._storage is None:
            return
        parsed = _parse_pending_consent_envelope(result)
        if parsed is None:
            return
        code, scopes = parsed
        scopes_str = " ".join(scopes) if scopes else None
        try:
            self._write_pending_consent(
                user_id, server_name, error_code=code, scopes_required=scopes_str
            )
        except Exception:
            log.warning(
                "mcp_pool.pending_consent_persist_failed user=%s server=%s code=%s",
                user_id,
                server_name,
                code,
                exc_info=True,
            )

    def _clear_pending_consent_sync(self, user_id: str, server_name: str) -> None:
        """Best-effort synchronous clear of a deferred-consent row on dispatch success.

        Called from the sync dispatchers when a dispatch SUCCEEDS, so a stale
        badge self-heals. This is the only clear path that covers oauth_obo: the
        oauth_user clears live in the token sweep (which skips obo) and the
        consent callback (which obo never runs), so without a success-side clear
        an obo pending row — written when the credential was missing — would
        persist forever after the user re-logs in.

        Deliberately NOT gated to oauth_obo: for oauth_user it also clears the
        dispatch-time transient badges written by
        :meth:`_record_pending_consent_best_effort` (non-interactive runs), which
        the sweep's ``_token_sweep_warned``-keyed clear never touches for a pair
        it didn't proactively warn. The TTL map below bounds the cost to one
        DELETE per pair per window, so the extra oauth_user coverage is close to
        free rather than redundant.

        Deduped via the ``_pending_consent_cleared`` TTL map so it is NOT an
        unconditional per-call SQL write: after a DB-confirmed DELETE the pair
        is skipped for ``_PENDING_CONSENT_CLEAR_TTL_SECONDS``, then the DELETE
        re-runs on the next success — bounding both the hot-path SQL rate (one
        DELETE per pair per TTL) and the staleness of a badge written by
        ANOTHER node after this node's last clear (a permanent cleared-set
        suppressed that clear forever). A failure observed on this node
        re-arms the pair immediately (``_write_pending_consent``); a node
        restart clears once on first success.
        """
        key = (user_id, server_name)
        now = time.monotonic()
        cleared_at = self._pending_consent_cleared.get(key)
        if cleared_at is not None and now - cleared_at < _PENDING_CONSENT_CLEAR_TTL_SECONDS:
            return  # cleared recently on this node — no SQL
        if self._storage is None:
            return
        try:
            self._storage.delete_mcp_pending_consent(user_id, server_name)
            self._mark_pending_consent_cleared(key, now)
        except Exception:
            log.debug(
                "mcp_pool.pending_consent_clear_failed user=%s server=%s",
                user_id,
                server_name,
                exc_info=True,
            )

    def _mark_pending_consent_cleared(self, key: tuple[str, str], now: float) -> None:
        """Record a DB-confirmed pending-consent DELETE in the TTL map.

        The single "prune-then-stamp" step shared by the two DB-confirmed clear
        sites (:meth:`_clear_pending_consent_sync` on the hot dispatch path and
        :meth:`_clear_pending_consent_best_effort` from the sweep) so the
        bookkeeping order can't drift between them.
        """
        self._prune_pending_consent_cleared(now)
        self._pending_consent_cleared[key] = now

    def _prune_pending_consent_cleared(self, now: float) -> None:
        """Drop aged entries when the TTL map grows large (memory hygiene).

        Runs from dispatch threads, so it snapshots ``items()`` and removes
        via ``pop`` — tolerant of concurrent same-map mutation (worst case a
        minor over/under-prune, never an exception). Correctness never
        depends on an entry being present: a dropped pair just re-runs one
        SQL DELETE on its next success.
        """
        if len(self._pending_consent_cleared) < _PENDING_CONSENT_CLEARED_MAX:
            return
        # Pass 1: drop expired entries (snapshot the items so concurrent mutation
        # can't raise mid-iteration; ``pop`` tolerates an already-removed key).
        for k, t in list(self._pending_consent_cleared.items()):
            if now - t >= _PENDING_CONSENT_CLEAR_TTL_SECONDS:
                self._pending_consent_cleared.pop(k, None)
        # Pass 2: only if still over the cap, drop the oldest half of what
        # REMAINS. Re-reading the map here means we sort just the live survivors
        # — never re-targeting keys pass 1 already removed.
        if len(self._pending_consent_cleared) >= _PENDING_CONSENT_CLEARED_MAX:
            survivors = sorted(self._pending_consent_cleared.items(), key=lambda kv: kv[1])
            for k, _ in survivors[: len(survivors) // 2]:
                self._pending_consent_cleared.pop(k, None)

    def _dispatch_pool_sync(
        self,
        *,
        user_id: str,
        server_name: str,
        original_name: str,
        arguments: dict[str, Any],
        server_row: dict[str, Any],
        timeout: int,
        is_interactive_for_consent: bool = True,
    ) -> str:
        """Synchronous wrapper for pool dispatch.

        Bridges sync caller → mcp-loop coroutine → result. Returns either
        the tool output string or a structured-error JSON string when
        token state forces the call to fail cleanly without raising
        (consent required, key mismatch, etc.). ``server_row`` is the
        row already resolved by ``_resolve_pool_target`` and is reused
        verbatim by ``_dispatch_pool`` to skip a duplicate DB hop.

        Retry-on-401: the 401 branch in :meth:`_dispatch_pool` raises
        :class:`_PoolDispatchRetryRequested` after refreshing the
        bearer. Catching the signal HERE — at the sync boundary — means
        the retry is scheduled via a fresh
        :func:`asyncio.run_coroutine_threadsafe` call, which runs the
        retry coroutine in a brand-new :class:`asyncio.Task` with no
        inherited anyio cancel-scope state from the prior connect's
        ``streamablehttp_client`` TaskGroup. An in-task retry inherits
        that scope state across :meth:`asyncio.Task.uncancel` and
        ``loop.create_task`` and surfaces ``CancelledError`` from inside
        the retry's own anyio scope. The retry-count ceiling is one;
        callers see consent_required if both attempts 401.

        The ``timeout`` is a wall-clock budget across both attempts —
        the retry's ``future.result`` window is reduced by however long
        the first attempt consumed before raising
        :class:`_PoolDispatchRetryRequested`. Without this, a slow
        first attempt followed by a stuck retry could double the
        caller-observed timeout.

        Structured-error returns (``mcp_consent_required`` /
        ``mcp_insufficient_scope`` / ``mcp_token_undecryptable_key_unknown``
        / ...) flow back from ``_dispatch_pool`` as a JSON string in the
        success-shape return slot. The session-layer ``_exec_mcp_tool``
        path keys on ``except Exception`` to render the dashboard's
        consent card — so we surface that JSON via ``RuntimeError`` here
        to drive the same path uniformly across tool / resource / prompt
        dispatchers. The wrap is gated on
        :func:`_is_structured_error` so a tool whose successful
        output happens to start with ``{"error":...`` is unaffected;
        only envelopes carrying an ``mcp_*`` code are converted.
        """
        assert self._loop is not None
        start = time.monotonic()
        try:
            result = self._run_pool_dispatch_attempt(
                retry_count=0,
                timeout=timeout,
                original_timeout=timeout,
                user_id=user_id,
                server_name=server_name,
                original_name=original_name,
                arguments=arguments,
                server_row=server_row,
            )
        except _PoolDispatchRetryRequested:
            # auth_401: refresh already happened on the prior task;
            # re-issue on a fresh task so the retry's anyio scope
            # state is independent of the prior connect's TaskGroup
            # teardown.
            remaining = max(1, int(timeout - (time.monotonic() - start)))
            result = self._run_pool_dispatch_attempt(
                retry_count=1,
                timeout=remaining,
                original_timeout=timeout,
                user_id=user_id,
                server_name=server_name,
                original_name=original_name,
                arguments=arguments,
                server_row=server_row,
            )
        if _is_structured_error(result):
            if not is_interactive_for_consent:
                self._record_pending_consent_best_effort(
                    user_id=user_id, server_name=server_name, result=result
                )
            raise RuntimeError(result)
        # Success clears any stale pending-consent badge (the obo self-heal path).
        self._clear_pending_consent_sync(user_id, server_name)
        return result

    def _run_pool_dispatch_attempt(
        self,
        *,
        retry_count: int,
        timeout: int,
        original_timeout: int,
        user_id: str,
        server_name: str,
        original_name: str,
        arguments: dict[str, Any],
        server_row: dict[str, Any],
    ) -> str:
        """Schedule one ``_dispatch_pool`` attempt and wait for the result.

        Split out of :meth:`_dispatch_pool_sync` so the two retry
        attempts share scheduling + timeout-bookkeeping without
        re-introducing the ``for retry_count in (0, 1)`` loop (which
        gave both attempts the full ``timeout`` and required an
        unreachable ``RuntimeError`` fallback).

        ``original_timeout`` is the wall-clock budget the caller
        requested; ``timeout`` is what's left for this specific
        attempt. The ``TimeoutError`` message reports the original so
        callers see the budget they set, not the trimmed window.
        """
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self._dispatch_pool(
                retry_count=retry_count,
                user_id=user_id,
                server_name=server_name,
                original_name=original_name,
                arguments=arguments,
                server_row=server_row,
            ),
            self._loop,
        )
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._cb_record_failure(server_name)
            raise TimeoutError(f"MCP tool call timed out after {original_timeout}s") from None

    def _dispatch_pool_resource_sync(
        self,
        *,
        user_id: str,
        server_name: str,
        uri: str,
        server_row: dict[str, Any],
        timeout: int,
        is_interactive_for_consent: bool = True,
    ) -> str:
        """Synchronous wrapper for pool resource read.

        Mirrors :meth:`_dispatch_pool_sync` for the resource path.
        Returns either the resource body or a structured-error JSON
        string when token state precludes the call.

        Retry-on-401 follows the same fresh-task scheduling pattern
        as the tool path — the dispatcher coroutine raises
        :class:`_PoolDispatchRetryRequested` after refreshing the
        bearer; we re-issue on a brand-new task so the retry's anyio
        cancel-scope state is independent of the prior connect's
        TaskGroup teardown.

        Structured-error returns are surfaced via ``RuntimeError`` —
        see :meth:`_dispatch_pool_sync` for the rationale; this keeps
        the agent-loop's ``except Exception`` handling uniform across
        tool and resource dispatchers.
        """
        assert self._loop is not None
        start = time.monotonic()
        try:
            result = self._run_pool_dispatch_resource_attempt(
                retry_count=0,
                timeout=timeout,
                original_timeout=timeout,
                user_id=user_id,
                server_name=server_name,
                uri=uri,
                server_row=server_row,
            )
        except _PoolDispatchRetryRequested:
            remaining = max(1, int(timeout - (time.monotonic() - start)))
            result = self._run_pool_dispatch_resource_attempt(
                retry_count=1,
                timeout=remaining,
                original_timeout=timeout,
                user_id=user_id,
                server_name=server_name,
                uri=uri,
                server_row=server_row,
            )
        if _is_structured_error(result):
            if not is_interactive_for_consent:
                self._record_pending_consent_best_effort(
                    user_id=user_id, server_name=server_name, result=result
                )
            raise RuntimeError(result)
        self._clear_pending_consent_sync(user_id, server_name)
        return result

    def _run_pool_dispatch_resource_attempt(
        self,
        *,
        retry_count: int,
        timeout: int,
        original_timeout: int,
        user_id: str,
        server_name: str,
        uri: str,
        server_row: dict[str, Any],
    ) -> str:
        """Schedule one resource dispatch attempt; same shape as
        :meth:`_run_pool_dispatch_attempt` for tools."""
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self._dispatch_pool_resource(
                retry_count=retry_count,
                user_id=user_id,
                server_name=server_name,
                uri=uri,
                server_row=server_row,
            ),
            self._loop,
        )
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._cb_record_failure(server_name)
            raise TimeoutError(f"MCP resource read timed out after {original_timeout}s") from None

    def _dispatch_pool_prompt_sync(
        self,
        *,
        user_id: str,
        server_name: str,
        original_name: str,
        arguments: dict[str, str] | None,
        server_row: dict[str, Any],
        timeout: int,
        is_interactive_for_consent: bool = True,
    ) -> list[dict[str, Any]]:
        """Synchronous wrapper for pool prompt invocation.

        Mirrors :meth:`_dispatch_pool_sync` for the prompt path. The
        async dispatcher returns either a list of expanded messages OR
        a structured-error JSON string (the failure path); to keep the
        return shape consistent with the static path
        (:meth:`get_prompt_sync`), errors are surfaced via
        :class:`RuntimeError` so the agent-loop's existing
        ``except Exception`` block at the call site renders the
        structured-error message. Embedding the JSON in a single-element
        message list would pollute the prompt protocol — open question 1
        in the plan.
        """
        assert self._loop is not None
        start = time.monotonic()
        try:
            result = self._run_pool_dispatch_prompt_attempt(
                retry_count=0,
                timeout=timeout,
                original_timeout=timeout,
                user_id=user_id,
                server_name=server_name,
                original_name=original_name,
                arguments=arguments,
                server_row=server_row,
            )
        except _PoolDispatchRetryRequested:
            remaining = max(1, int(timeout - (time.monotonic() - start)))
            result = self._run_pool_dispatch_prompt_attempt(
                retry_count=1,
                timeout=remaining,
                original_timeout=timeout,
                user_id=user_id,
                server_name=server_name,
                original_name=original_name,
                arguments=arguments,
                server_row=server_row,
            )
        if isinstance(result, str):
            # Structured-error path — surface as RuntimeError so the
            # agent-loop renders the JSON via its except-Exception
            # handler.
            if not is_interactive_for_consent:
                self._record_pending_consent_best_effort(
                    user_id=user_id, server_name=server_name, result=result
                )
            raise RuntimeError(result)
        self._clear_pending_consent_sync(user_id, server_name)
        return result

    def _run_pool_dispatch_prompt_attempt(
        self,
        *,
        retry_count: int,
        timeout: int,
        original_timeout: int,
        user_id: str,
        server_name: str,
        original_name: str,
        arguments: dict[str, str] | None,
        server_row: dict[str, Any],
    ) -> list[dict[str, Any]] | str:
        """Schedule one prompt dispatch attempt; returns either decoded
        messages or a structured-error string. Mirror of
        :meth:`_run_pool_dispatch_attempt` for prompts."""
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self._dispatch_pool_prompt(
                retry_count=retry_count,
                user_id=user_id,
                server_name=server_name,
                original_name=original_name,
                arguments=arguments,
                server_row=server_row,
            ),
            self._loop,
        )
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._cb_record_failure(server_name)
            raise TimeoutError(
                f"MCP prompt retrieval timed out after {original_timeout}s"
            ) from None

    async def _dispatch_pool(
        self,
        *,
        user_id: str,
        server_name: str,
        original_name: str,
        arguments: dict[str, Any],
        server_row: dict[str, Any],
        retry_count: int = 0,
    ) -> str:
        """Pool-side coroutine: resolve token, connect-or-reuse, dispatch.

        Returns either the textualized tool output or a structured-error
        JSON string when token state precludes dispatch. ``server_row``
        is supplied by ``_resolve_pool_target`` so this path doesn't
        re-issue the ``mcp_servers`` lookup; it's also pre-validated to
        be a pool-backed user-scoped auth type — ``oauth_user`` OR
        ``oauth_obo`` (``_pool_token_lookup`` below branches on it).

        ``retry_count`` is supplied by :meth:`_dispatch_pool_sync` and
        bounds the auth_401 refresh-and-retry to one re-issue. The first
        attempt (``retry_count == 0``) refreshes the token via
        ``force_refresh=True`` and raises
        :class:`_PoolDispatchRetryRequested` so the sync caller schedules
        the retry on a fresh :class:`asyncio.Task` (an in-task retry
        inherits anyio cancel-scope state from the prior connect's
        TaskGroup teardown and surfaces ``CancelledError`` from inside
        the retry's own anyio scope; the cross-task hop avoids that).
        At the ceiling (``retry_count == 1``) the auth_401 branch emits
        ``mcp_consent_required`` instead.
        """
        if self._app_state is None:
            raise RuntimeError("Pool dispatch requires set_app_state() to have been called")

        # Token-side errors (missing / decrypt / refresh-failed) bypass
        # the breaker entirely — the server itself is healthy. The breaker
        # gate runs only AFTER we have a usable access token; that way a
        # spurious cooldown-expired probe never lands here without ending
        # in either a real success or a real transport failure.
        #
        # ``retry_count >= 1`` forces the AS round-trip: the previous
        # attempt's auth_401 branch evicted the session and signalled
        # this retry; the local cached token is the one the AS just
        # rejected, so reading it back without ``force_refresh=True``
        # would re-attempt with the same (rejected) bearer.
        lookup, lookup_error = await self._pool_lookup_checked(
            server_row, user_id, server_name, force_refresh=retry_count > 0
        )
        if lookup_error is not None:
            return lookup_error
        access_token = lookup.token or ""

        # URL hygiene — pool dispatch transmits the per-user bearer; reject
        # plaintext schemes before they reach _connect_one_pool.
        url = str(server_row.get("url") or "")
        try:
            _validate_oauth_user_url(url)
        except ValueError as exc:
            log.warning(
                "mcp_pool.url_insecure server=%s scheme=%s",
                server_name,
                urllib.parse.urlparse(url).scheme,
            )
            return _structured_error(
                code="mcp_oauth_url_insecure",
                server=server_name,
                detail=str(exc),
            )

        self._cb_gate(server_name)

        cfg = _pool_cfg_from_row(server_row)
        key = (user_id, server_name)
        entry = await self._ensure_pool_entry(key)

        # See PoolEntryState.auth_capture for why the carrier is
        # entry-owned; _dispatch_pool_with_entry resets it under
        # open_lock before call_tool, we read it after.
        capture = entry.auth_capture
        try:
            result = await self._dispatch_pool_with_entry(
                entry=entry,
                key=key,
                cfg=cfg,
                access_token=access_token,
                original_name=original_name,
                arguments=arguments,
            )
        except BaseException as exc:
            classification = self._classify_failure(exc, capture=capture)
            if classification == "auth_401":
                self._evict_session(key)
                if retry_count == 0:
                    # 401 on the initial attempt: signal the sync caller
                    # to re-issue on a fresh :class:`asyncio.Task`. The
                    # retry's :meth:`_dispatch_pool` invocation runs the
                    # token lookup with ``force_refresh=True`` (the
                    # ``retry_count > 0`` branch above), guaranteeing
                    # the bearer attached to the retry's connect is
                    # different from the one the AS just rejected. The
                    # cross-task hop avoids the in-task anyio
                    # cancel-scope inheritance that surfaces a
                    # ``CancelledError`` from inside the retry's own
                    # ``streamablehttp_client`` TaskGroup — see
                    # :meth:`_dispatch_pool_sync` for the architectural
                    # rationale.
                    #
                    # exc_info=False: the underlying httpx exception
                    # carries ``request.headers["authorization"]`` with
                    # the rejected bearer; standard tracebacks don't
                    # render locals but Sentry / faulthandler hooks
                    # capture frame state. Structured fields below
                    # provide the diagnostic signal without the secret.
                    log.debug(
                        "mcp_pool.auth_401_initial server=%s user=%s exc=%s",
                        server_name,
                        user_id,
                        type(exc).__name__,
                    )
                    raise _PoolDispatchRetryRequested from None
                # retry_count == 1 — refreshed bearer also rejected;
                # emit consent_required so the user/operator re-grants.
                log.debug(
                    "mcp_pool.auth_401_retry_failed server=%s user=%s exc=%s",
                    server_name,
                    user_id,
                    type(exc).__name__,
                )
                # No catalog drop: refresh SUCCEEDED, so this 401 is
                # RS-side (JWKS lag / audience / skew), not a dead
                # grant — see _evict_session's docstring.
                return _structured_error(
                    code="mcp_consent_required",
                    server=server_name,
                    detail=_token_rejected_detail(server_row),
                    consent_url=_build_consent_url(server_row),
                )
            if classification == "auth_403":
                self._evict_session(key)
                log.debug(
                    "mcp_pool.auth_403 server=%s user=%s exc=%s",
                    server_name,
                    user_id,
                    type(exc).__name__,
                )
                return await self._handle_auth_403(
                    user_id=user_id,
                    server_name=server_name,
                    server_row=server_row,
                    capture=capture,
                )
            if classification == "transport":
                self._cb_record_failure(server_name)
                self._evict_session(key)
                # See auth_401 branch for why exc_info=False — pool
                # dispatch errors all share the same request object
                # whose headers carry the bearer.
                log.debug(
                    "mcp_pool.transport_failure server=%s user=%s exc=%s",
                    server_name,
                    user_id,
                    type(exc).__name__,
                )
                raise
            # protocol / other — don't trip the breaker.
            raise

        self._cb_record_success(server_name)
        return result

    async def _dispatch_pool_resource(
        self,
        *,
        user_id: str,
        server_name: str,
        uri: str,
        server_row: dict[str, Any],
        retry_count: int = 0,
    ) -> str:
        """Pool-side coroutine for resource reads (RFC §3.2).

        Mirror of :meth:`_dispatch_pool` for the resource path. Returns
        either the textualized resource content or a structured-error
        JSON string. Reuses the same token-classify, breaker-gate,
        URL-hygiene, carrier-race, classification and retry plumbing
        as the tool dispatcher; only the SDK call differs.
        """
        if self._app_state is None:
            raise RuntimeError("Pool dispatch requires set_app_state() to have been called")

        lookup, lookup_error = await self._pool_lookup_checked(
            server_row, user_id, server_name, force_refresh=retry_count > 0
        )
        if lookup_error is not None:
            return lookup_error
        access_token = lookup.token or ""

        url = str(server_row.get("url") or "")
        try:
            _validate_oauth_user_url(url)
        except ValueError as exc:
            log.warning(
                "mcp_pool.url_insecure server=%s scheme=%s",
                server_name,
                urllib.parse.urlparse(url).scheme,
            )
            return _structured_error(
                code="mcp_oauth_url_insecure",
                server=server_name,
                detail=str(exc),
            )

        self._cb_gate(server_name)

        cfg = _pool_cfg_from_row(server_row)
        key = (user_id, server_name)
        entry = await self._ensure_pool_entry(key)
        capture = entry.auth_capture
        try:
            sdk_result = await self._dispatch_pool_with_entry_call(
                entry=entry,
                key=key,
                cfg=cfg,
                access_token=access_token,
                sdk_call=lambda s: s.read_resource(uri),
            )
        except BaseException as exc:
            classification = self._classify_failure(exc, capture=capture)
            if classification == "auth_401":
                self._evict_session(key)
                if retry_count == 0:
                    log.debug(
                        "mcp_pool.auth_401_initial_resource server=%s user=%s exc=%s",
                        server_name,
                        user_id,
                        type(exc).__name__,
                    )
                    raise _PoolDispatchRetryRequested from None
                log.debug(
                    "mcp_pool.auth_401_retry_failed_resource server=%s user=%s exc=%s",
                    server_name,
                    user_id,
                    type(exc).__name__,
                )
                # No catalog drop: refresh SUCCEEDED, so this 401 is
                # RS-side (JWKS lag / audience / skew), not a dead
                # grant — see _evict_session's docstring.
                return _structured_error(
                    code="mcp_consent_required",
                    server=server_name,
                    detail=_token_rejected_detail(server_row),
                    consent_url=_build_consent_url(server_row),
                )
            if classification == "auth_403":
                self._evict_session(key)
                log.debug(
                    "mcp_pool.auth_403_resource server=%s user=%s exc=%s",
                    server_name,
                    user_id,
                    type(exc).__name__,
                )
                return await self._handle_auth_403(
                    user_id=user_id,
                    server_name=server_name,
                    server_row=server_row,
                    capture=capture,
                    kind="resource",
                )
            if classification == "transport":
                self._cb_record_failure(server_name)
                self._evict_session(key)
                log.debug(
                    "mcp_pool.transport_failure_resource server=%s user=%s exc=%s",
                    server_name,
                    user_id,
                    type(exc).__name__,
                )
                raise
            raise

        self._cb_record_success(server_name)
        return _decode_resource_result(sdk_result)

    async def _dispatch_pool_prompt(
        self,
        *,
        user_id: str,
        server_name: str,
        original_name: str,
        arguments: dict[str, str] | None,
        server_row: dict[str, Any],
        retry_count: int = 0,
    ) -> list[dict[str, Any]] | str:
        """Pool-side coroutine for prompt invocation (RFC §3.3).

        Mirror of :meth:`_dispatch_pool_resource` for the prompt path.
        Returns either the decoded list of expanded messages or a
        structured-error JSON string. Reuses the same token-classify,
        breaker-gate, URL-hygiene, carrier-race, classification and
        retry plumbing as the tool / resource dispatchers; only the SDK
        call differs.

        The dual return type (``list[dict[str, Any]] | str``) is shaped
        for the sync wrapper at :meth:`_dispatch_pool_prompt_sync` —
        it surfaces the structured-error string via
        :class:`RuntimeError` so the agent-loop's ``except Exception``
        block at the call site renders the JSON. Embedding error JSON
        in a single-element message list would pollute the prompt
        protocol (open question 1, plan §5).
        """
        if self._app_state is None:
            raise RuntimeError("Pool dispatch requires set_app_state() to have been called")

        lookup, lookup_error = await self._pool_lookup_checked(
            server_row, user_id, server_name, force_refresh=retry_count > 0
        )
        if lookup_error is not None:
            return lookup_error
        access_token = lookup.token or ""

        url = str(server_row.get("url") or "")
        try:
            _validate_oauth_user_url(url)
        except ValueError as exc:
            log.warning(
                "mcp_pool.url_insecure server=%s scheme=%s",
                server_name,
                urllib.parse.urlparse(url).scheme,
            )
            return _structured_error(
                code="mcp_oauth_url_insecure",
                server=server_name,
                detail=str(exc),
            )

        self._cb_gate(server_name)

        cfg = _pool_cfg_from_row(server_row)
        key = (user_id, server_name)
        entry = await self._ensure_pool_entry(key)
        capture = entry.auth_capture
        try:
            sdk_result = await self._dispatch_pool_with_entry_call(
                entry=entry,
                key=key,
                cfg=cfg,
                access_token=access_token,
                sdk_call=lambda s: s.get_prompt(original_name, arguments=arguments),
            )
        except BaseException as exc:
            classification = self._classify_failure(exc, capture=capture)
            if classification == "auth_401":
                self._evict_session(key)
                if retry_count == 0:
                    log.debug(
                        "mcp_pool.auth_401_initial_prompt server=%s user=%s exc=%s",
                        server_name,
                        user_id,
                        type(exc).__name__,
                    )
                    raise _PoolDispatchRetryRequested from None
                log.debug(
                    "mcp_pool.auth_401_retry_failed_prompt server=%s user=%s exc=%s",
                    server_name,
                    user_id,
                    type(exc).__name__,
                )
                # No catalog drop: refresh SUCCEEDED, so this 401 is
                # RS-side (JWKS lag / audience / skew), not a dead
                # grant — see _evict_session's docstring.
                return _structured_error(
                    code="mcp_consent_required",
                    server=server_name,
                    detail=_token_rejected_detail(server_row),
                    consent_url=_build_consent_url(server_row),
                )
            if classification == "auth_403":
                self._evict_session(key)
                log.debug(
                    "mcp_pool.auth_403_prompt server=%s user=%s exc=%s",
                    server_name,
                    user_id,
                    type(exc).__name__,
                )
                return await self._handle_auth_403(
                    user_id=user_id,
                    server_name=server_name,
                    server_row=server_row,
                    capture=capture,
                    kind="prompt",
                )
            if classification == "transport":
                self._cb_record_failure(server_name)
                self._evict_session(key)
                log.debug(
                    "mcp_pool.transport_failure_prompt server=%s user=%s exc=%s",
                    server_name,
                    user_id,
                    type(exc).__name__,
                )
                raise
            raise

        self._cb_record_success(server_name)
        return _decode_prompt_result(sdk_result)

    def _drop_session_and_stamp(self, key: tuple[str, str], entry: PoolEntryState) -> None:
        """Drop the entry's session AND its notification debounce stamp.

        The structural pairing for every teardown path (the same
        rationale as ``PoolEntryState.drop_session``): the stamp must
        not outlive the transport, so a reconnect's first
        ``list_changed`` refreshes immediately — a path that nulled the
        session without the pop silently re-opens the stale-stamp hole
        where a post-reconnect change is debounced against a
        pre-collapse stamp. Safe on an already-session-less entry
        (owner death after an eviction): ``drop_session`` is idempotent
        and the pop is a no-op.
        """
        entry.drop_session()
        for kind in _LIST_CHANGED_KINDS.values():
            self._last_pool_notification_refresh.pop((key, kind), None)

    def _evict_session(self, key: tuple[str, str]) -> None:
        """Drop the cached session on a pool entry; KEEP the catalog.

        Owner/streams left for reconnect. Auth/transport branches both call
        this — the next connect's ``_connect_one_pool`` tears down the stale
        owner lazily via its stale-entry guard (``_teardown_pool_entry``).
        Closing eagerly from here is incorrect: the transport cms live in the
        owner task and can only be unwound there (the one-cancel close
        protocol), which the next connect arranges. A session evicted with no
        following reconnect is reaped by idle eviction, which drives the same
        protocol.

        The catalog (``entry.tools`` / ``resources`` / ``prompts``) is
        deliberately RETAINED — the evict-session-keep-entry shape of
        ``_on_pool_owner_death`` (#836). Clearing it here removed the
        server's tools from the user's live sessions on the first failed
        dispatch (a transport blip, a 403, a double-401): the per-user
        maps rebuilt empty, the session-side ``is_mcp_tool`` gate
        closed, and with no re-prime path for a live session the tools
        never came back — which also made the breaker's half-open
        recovery and the consent / step-up cards unreachable (the model
        could no longer emit the name they need to fire). A session-less
        entry stays fully dispatchable: tool-name resolution never reads
        the catalog (``_resolve_pool_target`` is name + server-row
        based) and ``_dispatch_pool_with_entry`` connect-or-reuses,
        re-running discovery — post-reconnect drift self-corrects and
        the catalog-refresh notification fans out then.
        """
        evict = self._user_pool_entries.get(key)
        if evict is not None:
            self._drop_session_and_stamp(key, evict)

    def _evict_session_drop_catalog(self, key: tuple[str, str]) -> None:
        """Drop the cached session AND the entry's catalog contribution.

        The dead-grant flavor of :meth:`_evict_session`, used when the
        user's grant for this server is GONE — explicit disconnect
        (:meth:`evict_user_session`) or a dispatch that failed with the
        token-missing / grant-rejected class — so their live sessions
        SHOULD see the tools leave rather than keep offering a card for
        access that no longer exists (#836 cross-node convergence).
        Clearing the catalog also makes the eviction passes treat the
        entry as a droppable stub (``_entry_has_catalog`` is False), so
        it doesn't linger cooled behind a live listener.

        Session-drop prologue is delegated to :meth:`_evict_session` —
        owner/streams left for reconnect teardown exactly as there (the
        one-cancel close protocol). Callers that can race an in-flight
        connect must serialize via :meth:`_drop_catalog_locked` so a
        completing discovery can't republish the cleared catalog.

        ACCEPTED RESIDUAL (deliberate, after a rev-generation protocol
        proved buggier than the race it closed): a publisher already
        suspended across this drop — a ``list_changed`` refresh in
        flight, or a connect whose token was read pre-revocation and
        queued on ``open_lock`` ahead of us — can republish the catalog
        after the drop. The ghost is bounded and self-healing: any use
        of it fails the token lookup and re-schedules this drop
        (:meth:`_schedule_dead_grant_drop`, from dispatch AND priming),
        and a reconnected stale bearer dies at access-token expiry —
        the same bound every warm session already rides at revocation
        time.
        """
        self._evict_session(key)
        evict = self._user_pool_entries.get(key)
        if evict is None:
            return
        if not self._entry_has_catalog(evict):
            # Already catalog-less (repeat drop from a racing dispatch,
            # or a never-discovered stub) — nothing to clear, and a
            # zero-delta fan-out would wake every session of the user
            # to rebuild an unchanged list.
            return
        evict.tools = None
        evict.resources = None
        evict.prompts = None
        # Wake the user's sessions so their merged tool / resource /
        # prompt lists shrink now, not at the next turn boundary.
        self._rebuild_and_notify_user_catalogs(key[0])

    def _lookup_grant_dead(self, lookup: TokenLookupResult) -> bool:
        """Does this failed lookup prove the grant is GONE?

        Derived from :func:`_pool_lookup_verdict` — the classification
        has ONE source, so the consent card the user sees and the
        catalog-drop convergence can never disagree by kind. True only
        when the infrastructure that could know is actually wired: the
        obo lookup returns kind="missing" for an unconfigured token
        store / storage too, and dropping a catalog on a boot-ordering
        window would strand live sessions until a fresh prime.
        Transient refresh failures and decrypt failures keep the
        catalog: access still exists.
        """
        if getattr(self._app_state, "mcp_token_store", None) is None:
            return False
        if self._storage is None:
            return False
        return _pool_lookup_verdict(lookup) == "mcp_consent_required"

    def _observed_pool_session(self, user_id: str, server_name: str) -> Any:
        """Snapshot the entry's session for a dead-grant observation.

        MUST be called BEFORE the classified lookup's first await (the
        callers' contract with :meth:`_schedule_dead_grant_drop`): a
        snapshot taken after those awaits can capture a session the
        consent-completion prime connected mid-lookup, and the drop
        would then evict the just-restored catalog it was designed to
        spare — the session has to predate the failure to count as the
        pre-observation transport.
        """
        entry = self._user_pool_entries.get((user_id, server_name))
        return entry.session if entry is not None else None

    async def _pool_lookup_checked(
        self,
        server_row: dict[str, Any],
        user_id: str,
        server_name: str,
        *,
        force_refresh: bool,
    ) -> tuple[TokenLookupResult, str | None]:
        """Classified pool lookup with its convergence pairing, ordering-safe.

        The ONE copy of the dispatch-side lookup protocol (three
        dispatchers): the dead-grant observation is taken synchronously
        BEFORE the lookup's first await — a session the
        consent-completion prime connects DURING the lookup must read
        as re-consent evidence at drop time, not as pre-observation —
        and the failed-lookup render is paired with its convergence
        drop via :meth:`_pool_lookup_failure`. Structuring the ordering
        here makes it un-violable per dispatch site. Returns
        ``(lookup, error)``; ``error`` is ``None`` exactly when the
        lookup carries a usable bearer.
        """
        observed_session = self._observed_pool_session(user_id, server_name)
        lookup = await self._pool_token_lookup(
            server_row, user_id, server_name, force_refresh=force_refresh
        )
        return lookup, self._pool_lookup_failure(
            lookup, server_name, server_row, user_id, observed_session=observed_session
        )

    def _pool_lookup_failure(
        self,
        lookup: TokenLookupResult,
        server_name: str,
        server_row: dict[str, Any],
        user_id: str,
        *,
        observed_session: Any,
    ) -> str | None:
        """Render a failed pool lookup AND pair it with its convergence drop.

        The pairing is the contract (one helper, three dispatchers): a
        dispatcher that rendered the error without scheduling the drop
        would keep offering revoked tools behind a consent card — the
        cross-node non-convergence #836 fixes, silently split by
        dispatch surface. Returns ``None`` exactly when *lookup*
        carries a usable bearer. ``observed_session`` is the caller's
        PRE-lookup snapshot (:meth:`_observed_pool_session`).
        """
        lookup_error = _pool_lookup_error(lookup, server_name, server_row)
        if lookup_error is not None:
            self._schedule_dead_grant_drop(
                lookup, (user_id, server_name), observed_session=observed_session
            )
        return lookup_error

    def _schedule_dead_grant_drop(
        self,
        lookup: TokenLookupResult,
        key: tuple[str, str],
        *,
        observed_session: Any,
    ) -> None:
        """Converge live sessions with a durably-gone grant (#836).

        Called on the mcp-loop wherever a classified lookup fails — the
        three dispatchers, session-start priming, and the obo
        credential gate. When :meth:`_lookup_grant_dead` confirms the
        grant is gone (revoked, disconnected, or unlinked — not an
        infrastructure blip), the (user, server) catalog is dropped so
        the tools leave the user's live sessions instead of dangling
        behind a consent card for access that no longer exists.
        Re-consent restores them via the consent-completion prime; obo
        re-login via the capture prime.

        SCHEDULED, never awaited: the locked drop can park behind a
        same-key dispatch holding ``open_lock`` across its entire SDK
        call — awaiting here stalled token-side errors past the sync
        timeout and charged the breaker they are documented to bypass.
        ``_spawn_background`` tracks the task so shutdown cancels it.

        ``observed_session`` is REQUIRED and must be snapshotted by the
        caller BEFORE the classified lookup's first await
        (:meth:`_observed_pool_session`): a warm session that already
        existed at observation time predates the revocation and must be
        evicted (the #836 warm case — dispatches short-circuit on the
        failed lookup without ever touching the session, so no 401 path
        would converge it), while only a session that CHANGED after the
        observation proves a later successful connect. Snapshotting
        here, after the lookup returned, would capture a session the
        consent-completion prime connected mid-lookup and evict the
        just-restored catalog.

        Skips when there is provably nothing to converge — no entry, or
        a session-less entry with no catalog. At scale every
        unconsented server classifies as a dead grant on every prime;
        spawning a tracked no-op task per (user, unconsented server)
        for that is pure mcp-loop overhead. A catalog appearing AFTER
        this check implies a successful connect, i.e. a lookup that
        succeeded after this one failed.
        """
        if not self._lookup_grant_dead(lookup):
            return
        entry = self._user_pool_entries.get(key)
        if entry is None or (entry.session is None and not self._entry_has_catalog(entry)):
            return
        self._spawn_background(
            self._drop_catalog_locked(
                key, skip_if_connected=True, observed_session=observed_session
            ),
            f"dead-grant catalog drop for '{key[1]}'",
        )

    async def _drop_catalog_locked(
        self,
        key: tuple[str, str],
        *,
        skip_if_connected: bool = False,
        observed_session: Any = None,
    ) -> None:
        """Serialize a catalog drop against an in-flight connect.

        ``_evict_session_drop_catalog`` alone races
        ``_connect_one_pool``: a connect already past the token check
        publishes its discovery AFTER the drop, resurrecting a revoked
        server's catalog with no remaining path that ever clears it.
        ``open_lock`` is held across a same-key dispatch's ENTIRE SDK
        call (the carrier serialization), so this wait can be a full
        tool-call long — which is why every caller schedules it as a
        tracked background task rather than awaiting it inline.

        ``skip_if_connected`` is the dead-grant flavor's re-validation:
        a dispatch-scheduled drop can be parked long enough for the
        user to RE-CONSENT and the consent-completion prime to connect
        and publish a fresh catalog — clearing that would strand the
        just-restored tools (nothing re-primes until a new session).
        Re-consent evidence is a session DIFFERENT from
        ``observed_session`` — the one snapshotted when the dead grant
        was discovered (``_schedule_dead_grant_drop``): connects
        resolve their bearer through the classified lookup first, so a
        session created after the failed lookup implies a grant that
        was valid again at its connect. The session that was ALREADY
        live at observation time predates the revocation and is
        evicted along with the catalog — a warm transport is exactly
        how the #836 warm case dangles, since failed lookups
        short-circuit dispatch before any 401 could evict it. Callers
        without an observation leave ``observed_session=None``, which
        treats any live session as re-consent evidence (the cold
        case). The explicit-revocation path passes
        ``skip_if_connected=False``: it must clear even (especially)
        warm sessions.

        Bounded residual, accepted: if the entry is fully dropped,
        re-created, connected, and COOLED between the observation and
        this drop running, the stale drop clears that fresh cooled
        catalog — it self-heals at next use (the grant is alive, so
        dispatch/prime reconnects and republishes), and the window
        requires a full connect-and-cool cycle inside task-scheduling
        latency.
        """
        entry = self._user_pool_entries.get(key)
        if entry is None:
            return
        async with entry.open_lock:
            if (
                skip_if_connected
                and entry.session is not None
                and entry.session is not observed_session
            ):
                return
            self._evict_session_drop_catalog(key)

    async def _handle_auth_403(
        self,
        *,
        user_id: str,
        server_name: str,
        server_row: dict[str, Any],
        capture: _AuthCapture,
        kind: Literal["tool", "resource", "prompt"] = "tool",
    ) -> str:
        """Map a 403 + WWW-Authenticate into a structured error.

        ``error="insufficient_scope"`` becomes ``mcp_insufficient_scope``
        with the parsed ``scope=...`` set so the dashboard renderer
        can construct an authorize URL with the union of original +
        new scopes — re-consenting with the original scopes alone
        would loop because the AS would re-issue the same insufficient
        token. Other 403s become a generic per-operation forbidden
        code (``mcp_tool_call_forbidden`` / ``mcp_resource_read_forbidden``
        / ``mcp_prompt_get_forbidden``) so the dashboard renderer can
        special-case the message. The user lacks permission and a
        step-up wouldn't help; no retry.

        Both branches now emit an audit event via
        :func:`emit_oauth_failure_audit` carrying the ``kind`` and
        resolved error ``code`` so operators can distinguish tool-call
        vs resource-read vs prompt-get 403s for the same
        ``(user, server)`` and detect cross-tenant probing of generic
        forbidden surfaces.
        """
        header = capture.www_authenticate or ""
        error_token = parse_www_authenticate_error(header)
        if error_token == "insufficient_scope":
            scopes = parse_www_authenticate_scope(header)
            scopes = scopes[:MAX_INSUFFICIENT_SCOPE_REPORTED]
            await emit_oauth_failure_audit(
                app_state=self._app_state,
                user_id=user_id,
                server_name=server_name,
                server_row=server_row,
                kind=kind,
                code="mcp_insufficient_scope",
                scopes=scopes,
            )
            return _structured_error(
                code="mcp_insufficient_scope",
                server=server_name,
                detail=_pool_error_detail(server_row, "insufficient_scope", kind=kind),
                scopes_required=list(scopes),
                consent_url=_build_consent_url(server_row, scopes_required=list(scopes)),
            )
        forbidden_code = {
            "tool": "mcp_tool_call_forbidden",
            "resource": "mcp_resource_read_forbidden",
            "prompt": "mcp_prompt_get_forbidden",
        }[kind]
        forbidden_detail = {
            "tool": "Tool call forbidden by upstream policy.",
            "resource": "Resource read forbidden by upstream policy.",
            "prompt": "Prompt invocation forbidden by upstream policy.",
        }[kind]
        await emit_oauth_failure_audit(
            app_state=self._app_state,
            user_id=user_id,
            server_name=server_name,
            server_row=server_row,
            kind=kind,
            code=forbidden_code,
        )
        return _structured_error(
            code=forbidden_code,
            server=server_name,
            detail=forbidden_detail,
        )

    async def _dispatch_pool_with_entry(
        self,
        *,
        entry: PoolEntryState,
        key: tuple[str, str],
        cfg: dict[str, Any],
        access_token: str,
        original_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Hold ``entry.open_lock`` across connect-or-reuse AND ``call_tool``.

        Thin wrapper over :meth:`_dispatch_pool_with_entry_call` —
        passes a closure that invokes ``session.call_tool`` and
        decodes the SDK result. See the helper for the carrier-race
        rationale (which Phase 7 verified for the tool path; Phase 7b
        reuses the same shape for resources & prompts).

        **Why this wrapper exists** (revisited under Phase 7b pre-push
        review): the autouse fixture
        ``tests/test_mcp_pool_auth_introspection.py::_install_capture_intercept``
        monkeypatches this method to stash ``entry.auth_capture`` on
        ``mgr._test_active_capture`` for downstream call_tool stubs.
        Inlining the wrapper would require redirecting the monkeypatch
        to ``_dispatch_pool_with_entry_call`` (different kwargs shape)
        and re-validating every test that depends on the interception.
        Keeping the wrapper preserves a stable interception point on
        the codebase's hottest correctness path.
        """
        result = await self._dispatch_pool_with_entry_call(
            entry=entry,
            key=key,
            cfg=cfg,
            access_token=access_token,
            sdk_call=lambda s: s.call_tool(original_name, arguments),
        )
        return _decode_tool_result(result)

    async def _dispatch_pool_with_entry_call(
        self,
        *,
        entry: PoolEntryState,
        key: tuple[str, str],
        cfg: dict[str, Any],
        access_token: str,
        sdk_call: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Hold ``entry.open_lock`` across connect-or-reuse AND ``sdk_call``.

        Generalisation of the Phase 7 ``_dispatch_pool_with_entry``
        machinery (RFC §3.2): ``sdk_call`` is a closure that takes the
        SDK ``ClientSession`` and returns an awaitable. The dispatcher
        passes ``lambda s: s.call_tool(name, args)`` for tools,
        ``lambda s: s.read_resource(uri)`` for resources, and
        ``lambda s: s.get_prompt(name, arguments=args)`` for prompts.

        Lock held across the SDK call because the entry's
        ``_AuthCapture`` is keyed off the httpx event hook; releasing
        the lock would let a concurrent same-(user, server) dispatch
        overwrite the carrier mid-flight and attribute one caller's
        auth failure to another. Holding the lock serialises
        same-(user, server) calls — acceptable because that contention
        scenario is rare in practice.

        Reset of ``entry.auth_capture`` and ``entry.auth_fired_event``
        happens under the lock before the SDK call — see
        :class:`PoolEntryState` for why the carrier is entry-owned.

        ``entry.in_flight`` accounting is preserved for the eviction
        interlock — belt-and-braces since ``open_lock.locked()``
        already signals "do not evict", but the in-flight counter
        remains the source of truth for
        :meth:`_close_pool_entry_if_idle`.
        """
        async with entry.open_lock:
            entry.last_used = time.monotonic()
            self._user_pool_last_used[key] = entry.last_used
            entry.auth_capture.status = None
            entry.auth_capture.www_authenticate = None
            entry.auth_fired_event.clear()
            session = entry.session
            if (
                session is not None
                and entry.bound_token is not None
                and entry.bound_token != access_token
            ):
                # The stored token refreshed since this session connected. The
                # pooled httpx client's Authorization header is frozen at connect
                # (_connect_one_pool), so a warm session would replay the now-
                # stale bearer and eat a guaranteed upstream 401 before the
                # auth_401 retry could heal it. Proactively reconnect to rebind
                # the current token: _connect_one_pool tears down the old
                # owner/streams, and entry.tools is retained (catalog intact), so
                # this is a transparent in-place token rotation — attempt #0 now
                # carries a valid bearer.
                #
                # ``bound_token is not None`` gate: only rotate when this session
                # was established through ``_connect_one_pool`` (which records the
                # bearer). A directly-injected session (None bind token) is left
                # to the existing auth_401 retry path — and is the shape unit
                # tests use, so this avoids spurious reconnects there.
                log.debug("mcp_pool.token_rotated_reconnect user=%s server=%s", key[0], key[1])
                session = None
            if session is None:
                # Lazy connect — also covers post-eviction recovery.
                fresh = await self._connect_one_pool(
                    key,
                    cfg,
                    access_token,
                    auth_capture=entry.auth_capture,
                    auth_fired_event=entry.auth_fired_event,
                )
                session = fresh.session
                if session is None:
                    raise RuntimeError(f"Pool connect for {key!r} produced no session")
            entry.in_flight += 1
            try:
                # Race ``sdk_call`` against the carrier's fired event.
                # Without this race, an upstream 4xx on a REUSED session
                # never propagates back through the SDK call: the SDK's
                # ``_receive_loop`` is in BaseSession's TaskGroup, nested
                # inside ``streamablehttp_client``'s TaskGroup. When
                # the spawned ``handle_request_async`` task raises
                # ``HTTPStatusError``, the outer TaskGroup cancels
                # ``_receive_loop`` mid-finally, before it can deliver
                # ``CONNECTION_CLOSED`` to the response stream's waiting
                # receiver. anyio's ``send_nowait`` skips waiters with
                # pending cancellation; here the dispatcher's task has
                # NO pending cancellation (it was created by a fresh
                # ``run_coroutine_threadsafe`` and is not in the
                # streamablehttp_client cancel-scope chain), so the
                # send delivers but the receiver never wakes — the
                # waiter's Event is set on a stale state. Result: a
                # forever-hung ``response_stream_reader.receive()``.
                # The carrier-fired event lets us short-circuit before
                # the SDK's hang manifests.
                async def _await_sdk_call() -> Any:
                    return await sdk_call(session)

                call_task: asyncio.Task[Any] = asyncio.create_task(_await_sdk_call())
                fired_task = asyncio.create_task(entry.auth_fired_event.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {call_task, fired_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    # Cancel-and-await both losers. Awaiting cancelled
                    # tasks here pins the broken session's streams
                    # against the auth_401 retry's
                    # ``_teardown_pool_entry`` teardown — without it the
                    # cancelled ``call_task`` could keep touching the
                    # SDK's stream state concurrently with the new
                    # ``_connect_one_pool``'s owner unwind.
                    # ``BaseException`` covers both the
                    # ``CancelledError`` we asked for and any
                    # ``BaseExceptionGroup`` the SDK's anyio
                    # TaskGroup may wrap on teardown.
                    for task in (call_task, fired_task):
                        if not task.done():
                            task.cancel()
                    for task in (call_task, fired_task):
                        with contextlib.suppress(BaseException):
                            await task
                if call_task in done:
                    return call_task.result()
                # Hook captured 4xx before the SDK call returned. The
                # SDK won't propagate the failure through the call, so
                # eagerly tear down the session and raise a sentinel
                # that the dispatcher's ``_classify_failure`` will
                # resolve via the carrier (which holds the captured
                # status).
                raise _CarrierAuthSignal()
            finally:
                entry.in_flight -= 1

    # -- resource read -------------------------------------------------------

    def _match_template(self, uri: str, *, user_id: str | None = None) -> tuple[str, str] | None:
        """Find the longest matching template prefix for an expanded URI.

        Returns ``(server_name, template_uri)`` or *None* if no match.
        Uses ``startswith`` longest-prefix matching.

        Per scope decision 0.1, when ``user_id is not None`` the
        per-user template index is consulted FIRST; static templates
        only run as a fallback. This keeps a user's own scoped templates
        authoritative within their namespace and avoids surprising
        static→pool fallthrough that would attach a per-user bearer
        to a server the operator believed was the same name.
        """
        best: tuple[str, str] | None = None
        best_len = 0
        if user_id is not None:
            for prefix, mapping in (self._user_template_prefixes.get(user_id) or {}).items():
                if uri.startswith(prefix) and len(prefix) > best_len:
                    best = mapping
                    best_len = len(prefix)
            if best is not None:
                return best
        for prefix, mapping in self._template_prefixes.items():
            if uri.startswith(prefix) and len(prefix) > best_len:
                best = mapping
                best_len = len(prefix)
        return best

    def read_resource_sync(
        self,
        uri: str,
        *,
        user_id: str | None = None,
        timeout: int = 120,
        is_interactive_for_consent: bool = True,
    ) -> str:
        """Read a resource by URI synchronously (blocks the calling thread).

        Returns text content for ``TextResourceContents``, or base64 data
        for ``BlobResourceContents``.

        When ``user_id`` is supplied AND the URI resolves to a pool
        entry (per scope decision 0.1, per-user-first), dispatch goes
        through the per-(user, server) pool with the same 401 / 403 /
        consent-required handling as :meth:`call_tool_sync`. Otherwise
        the call takes the byte-identical static path (invariant 1).
        """
        # Phase 7b — per-user-first pool dispatch.
        if user_id and self._app_state is not None and self._storage is not None:
            pool_target = self._resolve_pool_target_resource(uri, user_id)
            if pool_target is not None:
                return self._dispatch_pool_resource_sync(
                    user_id=user_id,
                    server_name=pool_target[0],
                    uri=pool_target[1],
                    server_row=pool_target[2],
                    timeout=timeout,
                    is_interactive_for_consent=is_interactive_for_consent,
                )

        mapping = self._resource_map.get(uri)
        if mapping is None:
            # Fall back to template prefix matching for expanded URIs
            mapping = self._match_template(uri)
        if mapping is None:
            # Per-user resources whose static lookup misses fall through here
            # only when the user has no pool entry for the server. A pool
            # entry exists in ``_user_resource_map`` only AFTER discovery; the
            # pool resolver tried it above and returned None (no oauth_user
            # row).
            if (
                user_id is not None
                and (self._user_resource_map.get(user_id) or {}).get(uri) is not None
            ):
                # Per-user map carries this URI but the resolver could not
                # match it to an oauth_user server row; this is an internal
                # inconsistency.
                raise ValueError(f"Unknown MCP resource: {uri} (per-user map / DB mismatch)")
            raise ValueError(f"Unknown MCP resource: {uri}")
        server_name, _ = mapping

        self._cb_gate(server_name)

        state = self._static_servers.get(server_name)
        session = state.session if state is not None else None
        if session is None:
            session = self._cb_auto_reconnect(server_name)
        assert self._loop is not None

        future = asyncio.run_coroutine_threadsafe(
            self._static_session_op(server_name, session.read_resource(uri)), self._loop
        )
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._cb_record_failure(server_name)
            raise TimeoutError(f"MCP resource read timed out after {timeout}s") from None
        except Exception as exc:
            self._record_and_evict_on_dead_transport(server_name, exc)
            raise

        self._cb_record_success(server_name)
        return _decode_resource_result(result)

    # -- prompt invocation ---------------------------------------------------

    def get_prompt_sync(
        self,
        prefixed_name: str,
        arguments: dict[str, str] | None = None,
        *,
        user_id: str | None = None,
        timeout: int = 30,
        is_interactive_for_consent: bool = True,
    ) -> list[dict[str, Any]]:
        """Invoke an MCP prompt synchronously and return expanded messages.

        Returns a list of ``{role: str, content: str}`` dicts.

        When ``user_id`` is supplied AND ``prefixed_name`` resolves to a
        pool entry, dispatch goes through the per-(user, server) pool
        with the same 401 / 403 / consent-required handling as
        :meth:`call_tool_sync`. Otherwise the call takes the byte-
        identical static path (invariant 1).

        Pool error path: structured-error responses (consent required,
        decrypt failure, insufficient scope, etc.) are surfaced via
        :class:`RuntimeError` so the agent-loop's existing
        ``except Exception`` block at the call site renders the error
        message. The return type stays ``list[dict[str, Any]]`` because
        embedding error JSON in a single-element message list would
        pollute the prompt protocol.
        """
        # Phase 7b — pool dispatch (per-user-first, scope decision 0.1).
        static_mapping = self._prompt_map.get(prefixed_name)
        static_server: str | None = None
        static_original: str | None = None
        if static_mapping is not None:
            static_server, static_original = static_mapping

        if user_id and self._app_state is not None and self._storage is not None:
            pool_target = self._resolve_pool_target_prompt(
                prefixed_name, static_server, static_original
            )
            if pool_target is not None:
                return self._dispatch_pool_prompt_sync(
                    user_id=user_id,
                    server_name=pool_target[0],
                    original_name=pool_target[1],
                    arguments=arguments,
                    server_row=pool_target[2],
                    timeout=timeout,
                    is_interactive_for_consent=is_interactive_for_consent,
                )

        if static_mapping is None:
            # Per-user prompts whose static lookup misses fall through here
            # only when the user has no pool entry for the server. A pool
            # entry exists in ``_user_prompt_map`` only AFTER discovery; the
            # pool resolver tried it above and returned None (no oauth_user
            # row).
            if (
                user_id is not None
                and (self._user_prompt_map.get(user_id) or {}).get(prefixed_name) is not None
            ):
                # Per-user map carries this prefixed name but the resolver
                # could not match it to an oauth_user server row; this is
                # an internal inconsistency.
                raise ValueError(
                    f"Unknown MCP prompt: {prefixed_name} (per-user map / DB mismatch)"
                )
            raise ValueError(f"Unknown MCP prompt: {prefixed_name}")
        server_name, original_name = static_mapping

        self._cb_gate(server_name)

        state = self._static_servers.get(server_name)
        session = state.session if state is not None else None
        if session is None:
            session = self._cb_auto_reconnect(server_name)
        assert self._loop is not None

        future = asyncio.run_coroutine_threadsafe(
            self._static_session_op(
                server_name, session.get_prompt(original_name, arguments=arguments)
            ),
            self._loop,
        )
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._cb_record_failure(server_name)
            raise TimeoutError(f"MCP prompt retrieval timed out after {timeout}s") from None
        except Exception as exc:
            self._record_and_evict_on_dead_transport(server_name, exc)
            raise

        self._cb_record_success(server_name)
        return _decode_prompt_result(result)

    def evict_user_session(self, user_id: str, server_name: str) -> None:
        """Drop the cached pool session AND catalog for ``(user_id, server_name)``.

        Sync entry point for the OAuth revoke handler: the user
        explicitly disconnected the server, so the session is dropped
        and — unlike the dispatch-failure eviction (#836) — the user's
        catalog view of the server is removed and their live sessions
        notified (:meth:`_evict_session_drop_catalog`). Idempotent —
        a missing key is a silent no-op. Fire-and-forget: schedules
        onto the mcp-loop and returns immediately without waiting for
        the future. Best-effort: a closed loop or scheduling failure
        logs at info level; never raises.
        """
        if self._loop is None:
            return
        key = (user_id, server_name)

        async def _spawn_tracked() -> None:
            # ``_spawn_background`` is loop-thread-only — hop first.
            # Tracking matters: the locked drop can park behind a
            # same-key dispatch's long SDK call, and an untracked task
            # still pending at shutdown is abandoned with "Task was
            # destroyed but it is pending!" noise; tracked tasks are
            # cancelled by shutdown().
            self._spawn_background(
                self._drop_catalog_locked(key),
                f"revocation catalog drop for '{server_name}'",
            )

        try:
            # Locked: an in-flight connect completing its discovery
            # after an unserialized drop would republish (resurrect)
            # the revoked catalog with nothing left to clear it.
            asyncio.run_coroutine_threadsafe(_spawn_tracked(), self._loop)
        except RuntimeError as exc:
            log.info(
                "mcp_pool.evict_user_session_failed server=%s user=%s error=%s",
                server_name,
                user_id,
                type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# Pool helpers (module-level)
# ---------------------------------------------------------------------------


def _decode_tool_result(result: Any) -> str:
    """Render an MCP ``tools/call`` result into the string the agent sees.

    Walks ``result.content`` collecting text parts and labelling binary
    parts; an ``isError`` result is prefixed with ``Error: `` so the
    LLM can narrate the failure. Shared by the static and pool dispatch
    paths.
    """
    texts: list[str] = []
    for item in result.content:
        if hasattr(item, "text"):
            texts.append(item.text)
        elif hasattr(item, "data"):
            mime = getattr(item, "mimeType", "binary")
            texts.append(f"[{mime} data, {len(item.data)} bytes]")
        else:
            texts.append(str(item))
    output = "\n".join(texts) if texts else "(no output)"
    if getattr(result, "isError", False):
        output = f"Error: {output}"
    return output


def _decode_resource_result(result: Any) -> str:
    """Render an MCP ``resources/read`` result into the string the agent sees.

    Walks ``result.contents`` collecting text parts (TextResourceContents)
    and base64 data (BlobResourceContents). Shared by the static and pool
    resource-read paths.
    """
    parts: list[str] = []
    for item in result.contents:
        if hasattr(item, "text"):
            parts.append(item.text)
        elif hasattr(item, "blob"):
            parts.append(item.blob)
        else:
            parts.append(str(item))
    return "\n".join(parts) if parts else "(empty resource)"


def _decode_prompt_result(result: Any) -> list[dict[str, Any]]:
    """Render an MCP ``prompts/get`` result into the list-of-messages
    the agent sees. Shared by the static and pool prompt-get paths.
    """
    messages: list[dict[str, Any]] = []
    for msg in result.messages:
        content = msg.content
        text = content.text if hasattr(content, "text") else str(content)
        messages.append({"role": msg.role, "content": text})
    return messages


def _structured_error(
    *,
    code: str,
    server: str,
    detail: str,
    scopes_required: list[str] | None = None,
    consent_url: str | None = None,
) -> str:
    """Encode a pool-dispatch failure as a JSON string.

    Returned to the agent through ``_exec_mcp_tool`` so the LLM can
    narrate "tool unavailable, consent required" rather than crashing
    the workstream. Schema covers ``mcp_consent_required``,
    decrypt-failure (``mcp_token_undecryptable_key_unknown``), and the
    ``mcp_insufficient_scope`` step-up shape.

    ``scopes_required`` is omitted from the payload when ``None`` —
    the dashboard renderer keys on its presence to construct an
    authorize URL with the union of original + new scopes.

    ``consent_url`` is omitted when ``None``. When present it carries a
    relative ``/v1/api/mcp/oauth/start?...`` path the dashboard can
    open in a popup. Only emitted for user-actionable codes
    (``mcp_consent_required``, ``mcp_insufficient_scope``); operator-
    actionable codes (key-unknown, URL-insecure, generic forbidden)
    intentionally omit it because re-consent doesn't help.

    Operator-actionable encryption-key fingerprints are intentionally
    NOT included in this payload: they are already captured server-side
    via :meth:`MCPTokenStore._audit_decrypt_failure`, and exposing them
    to the LLM (and through it to the model provider) would be
    unnecessary disclosure.
    """
    err: dict[str, Any] = {
        "code": code,
        "server": server,
        "detail": detail,
    }
    if scopes_required is not None:
        err["scopes_required"] = scopes_required
    if consent_url is not None:
        err["consent_url"] = consent_url
    return json.dumps({"error": err})


# Structured-error codes that represent a deferred-consent need.  When
# encountered on a non-interactive call (chat / scheduled), the sync
# dispatcher persists a row to ``mcp_pending_consent`` so the dashboard
# badge can surface the deferred work later.  Operator-actionable codes
# (key-unknown, url-insecure, *_forbidden) are intentionally excluded —
# the user cannot resolve them by completing a consent flow.
_PENDING_CONSENT_PERSIST_CODES: frozenset[str] = frozenset(
    {"mcp_consent_required", "mcp_insufficient_scope"}
)


def _parse_pending_consent_envelope(
    result: str,
) -> tuple[str, list[str] | None] | None:
    """Extract ``(error_code, scopes_required)`` from a structured-error JSON.

    Returns ``None`` when the envelope's ``code`` is not in
    :data:`_PENDING_CONSENT_PERSIST_CODES`.  Callers should already have
    gated on :func:`_is_structured_error`; this helper deliberately
    re-parses (cheap on the failure path) rather than threading the
    decoded dict through the sync-dispatcher hot path.

    Defends against non-dict JSON values (``null``, strings, numbers)
    via the same ``isinstance(decoded, dict)`` guard
    :func:`_is_structured_error` uses, so a misuse from a future caller
    that bypasses the structured-error contract surfaces as a clean
    ``None`` rather than an ``AttributeError`` propagating out of the
    sync dispatcher's hot path.
    """
    try:
        decoded = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    err = decoded.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code", "")
    if code not in _PENDING_CONSENT_PERSIST_CODES:
        return None
    scopes = err.get("scopes_required")
    if isinstance(scopes, list):
        # Defense-in-depth scope filter — production paths construct
        # this list via ``parse_www_authenticate_scope`` which already
        # validates and caps, but the helper is reusable; re-applying
        # the predicate here forecloses any future caller that bypasses
        # the upstream filter from landing attacker-controlled bytes in
        # ``mcp_pending_consent.scopes_required``.  Type-filter BEFORE
        # ``is_valid_scope_token`` so non-string entries (``None``,
        # ints) don't slip through as their ``str()`` repr (e.g.
        # ``None`` → ``"None"`` passes the ASCII grammar).  Cap mirrors
        # ``MAX_INSUFFICIENT_SCOPE_REPORTED`` semantics.
        cleaned = [s for s in scopes if isinstance(s, str) and is_valid_scope_token(s)]
        return code, cleaned[:MAX_INSUFFICIENT_SCOPE_REPORTED]
    return code, None


def _is_structured_error(result: str) -> bool:
    """Return True if *result* parses as a :func:`_structured_error` envelope.

    Used by :meth:`MCPClientManager._dispatch_pool_sync` /
    :meth:`MCPClientManager._dispatch_pool_resource_sync` to convert the
    dispatcher's success-shape return into a raised ``RuntimeError``, so
    the session-layer ``except`` branch fires uniformly across tool /
    resource / prompt dispatchers. The prompt path uses an
    ``isinstance(result, str)`` hack that doesn't generalize because
    tools and resources return ``str`` on success too.
    """
    if not isinstance(result, str) or not result.startswith('{"error":'):
        return False
    try:
        decoded = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(decoded, dict):
        return False
    err = decoded.get("error")
    if not isinstance(err, dict):
        return False
    code = err.get("code")
    return isinstance(code, str) and code.startswith("mcp_")


def _build_consent_url(
    server_row: dict[str, Any],
    *,
    scopes_required: list[str] | None = None,
) -> str | None:
    """Build a consent URL the dashboard can open in a popup.

    Returns ``None`` when ``server_row`` is not configured for
    ``auth_type=oauth_user`` (defensive — a structured error for a
    static-auth server should not advertise a consent flow).

    The ``return_url`` query param is intentionally not baked in: the
    dashboard JS appends ``window.location.href`` at click time,
    matching the existing admin.js connect-button pattern. The scope
    set is passed through so the step-up flow can union with the
    server's configured scopes server-side at /start.
    """
    if server_row.get("auth_type") != "oauth_user":
        return None
    server_name = str(server_row.get("name") or "")
    if not server_name:
        return None
    qs = "server=" + urllib.parse.quote(server_name, safe="")
    if scopes_required:
        scopes_str = " ".join(scopes_required)
        qs += "&scopes=" + urllib.parse.quote(scopes_str, safe="")
    return f"/v1/api/mcp/oauth/start?{qs}"


# User-facing remediation copy for every (auth model, failure situation) a
# pool-backed dispatch can surface. Consolidated into ONE table so the
# oauth_user-vs-oauth_obo split lives in a single place instead of a branch
# fanned across four helpers: a lookup situation that must show obo re-login /
# admin guidance can't silently keep oauth_user's "re-consent at a per-server
# flow that doesn't exist for obo" copy — the exact wrong-remediation bug this
# feature's review caught repeatedly. ``{kind}`` / ``{kind_cap}`` are filled per
# call (only the insufficient_scope rows use them; other rows ignore them).
_POOL_ERROR_DETAIL: dict[tuple[str, str], str] = {
    ("oauth_user", "missing"): "No token for user. Consent flow required.",
    ("oauth_obo", "missing"): (
        "No sign-in credential for this account. Sign in to Turnstone again to reconnect."
    ),
    ("oauth_user", "refresh_failed"): "Refresh token rejected. Re-consent required.",
    ("oauth_obo", "refresh_failed"): (
        "Sign-in credential was rejected for this server. Sign in to Turnstone "
        "again; if it keeps failing, your administrator may need to grant access."
    ),
    ("oauth_user", "insufficient_scope"): (
        "{kind_cap} requires elevated scopes. Re-consent flow with new scopes required."
    ),
    ("oauth_obo", "insufficient_scope"): (
        "This {kind} needs additional permissions your sign-in token does not "
        "carry. Ask your administrator to grant the required access (add the "
        "scope to the server, or widen your delegated permissions at the "
        "identity provider)."
    ),
    ("oauth_user", "token_rejected"): "Refreshed token still rejected. Re-consent required.",
    ("oauth_obo", "token_rejected"): (
        "Server rejected a freshly issued sign-in token. This usually means the "
        "server's audience setting doesn't match what the server expects — ask "
        "your administrator to check it."
    ),
}


def _pool_error_detail(server_row: dict[str, Any], situation: str, *, kind: str = "") -> str:
    """Remediation copy for *situation* on *server_row*, chosen by auth model.

    Single lookup into :data:`_POOL_ERROR_DETAIL` — the ONE place the
    oauth_user-vs-oauth_obo decision is made — so no dispatch site can pair a
    situation with the wrong auth model's copy. ``oauth_obo`` rows never have a
    per-server consent flow (``_build_consent_url`` returns None), so their copy
    points at a re-login / administrator remedy rather than a dead-end consent
    card; everything else is treated as ``oauth_user``.
    """
    model = "oauth_obo" if server_row.get("auth_type") == "oauth_obo" else "oauth_user"
    template = _POOL_ERROR_DETAIL[(model, situation)]
    # Only the insufficient_scope rows carry {kind}/{kind_cap}; the rest are
    # plain strings. Format ONLY when a kind is supplied so a future message
    # containing a literal brace (a scope/JSON example) can't raise inside this
    # error-rendering path and turn a clean structured error into a 500.
    if kind:
        return template.format(kind=kind, kind_cap=kind.capitalize())
    return template


def _consent_missing_detail(server_row: dict[str, Any]) -> str:
    """Detail for a ``missing`` token lookup (see :func:`_pool_error_detail`).

    Kept as a named wrapper (unlike the single-use situations, which call
    :func:`_pool_error_detail` directly) because it has multiple callers.
    """
    return _pool_error_detail(server_row, "missing")


def _token_rejected_detail(server_row: dict[str, Any]) -> str:
    """Detail for a 401 that survived one forced refresh (see :func:`_pool_error_detail`).

    Named wrapper because the three sync dispatchers each surface it.
    """
    return _pool_error_detail(server_row, "token_rejected")


def _pool_lookup_error(
    lookup: TokenLookupResult, server_name: str, server_row: dict[str, Any]
) -> str | None:
    """Map a failed pool token lookup to its structured error; ``None`` on success.

    Single copy of the lookup-kind → structured-error RENDERING shared by
    the tool / resource / prompt dispatchers — previously three hand-synced
    copies that every auth-model change had to edit in lockstep. Returns
    ``None`` exactly when *lookup* carries a non-empty bearer. The
    CLASSIFICATION lives in :func:`_pool_lookup_verdict`; this function
    only chooses per-kind detail copy for the code it returns.
    """
    # Literal ``code=`` strings in each branch (not ``code=verdict``) so the
    # consent-url sibling audit (tests/test_mcp_consent_url_sibling_audit.py)
    # keeps seeing these sites — a variable code is invisible to its scan.
    verdict = _pool_lookup_verdict(lookup)
    if verdict is None:
        return None
    if verdict == "mcp_token_undecryptable_key_unknown":
        return _structured_error(
            code="mcp_token_undecryptable_key_unknown",
            server=server_name,
            detail=(
                "Stored token cannot be decrypted by any installed encryption key. "
                "Operator action required."
            ),
        )
    if verdict == "mcp_refresh_unavailable":
        # Transient refresh failure (AS/network blip) — the token was kept;
        # a retry may succeed once the AS recovers. Retryable, NOT re-consent.
        return _structured_error(
            code="mcp_refresh_unavailable",
            server=server_name,
            detail="Token refresh temporarily failed; please retry.",
        )
    # verdict == "mcp_consent_required": pick the detail by kind.
    if lookup.kind == "refresh_failed":
        # The ``mcp_server.oauth.token_revoked`` audit was already emitted by
        # the classified lookup when it deleted the row; no second audit here.
        detail = _pool_error_detail(server_row, "refresh_failed")
    else:
        # kind == "missing", or the barely-reachable empty-token fallback —
        # auth-model-aware so an obo row never shows per-server-consent copy
        # + a null consent_url.
        detail = _consent_missing_detail(server_row)
    return _structured_error(
        code="mcp_consent_required",
        server=server_name,
        detail=detail,
        consent_url=_build_consent_url(server_row),
    )


_PoolLookupVerdict = Literal[
    "mcp_consent_required",
    "mcp_token_undecryptable_key_unknown",
    "mcp_refresh_unavailable",
]


def _pool_lookup_verdict(lookup: TokenLookupResult) -> _PoolLookupVerdict | None:
    """Classify a pool token lookup outcome as a structured-error code.

    THE single classification of failed lookups: ``_pool_lookup_error``
    renders it, and ``MCPClientManager._lookup_grant_dead`` derives the
    catalog-drop decision from it (``mcp_consent_required`` == the grant
    is gone) — so the card the user sees and the convergence behavior
    can never disagree by kind. Returns ``None`` exactly when *lookup*
    carries a non-empty bearer.
    """
    if lookup.kind == "missing":
        return "mcp_consent_required"
    if lookup.kind == "decrypt_failure":
        return "mcp_token_undecryptable_key_unknown"
    if lookup.kind == "refresh_failed_transient":
        return "mcp_refresh_unavailable"
    if lookup.kind == "refresh_failed":
        return "mcp_consent_required"
    # kind == "token"
    if not (lookup.token or ""):
        return "mcp_consent_required"
    return None


def _pool_cfg_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build a streamable-http MCP-client cfg from an ``mcp_servers`` row.

    Pool servers are required to be HTTP-transport (auth_type=oauth_user
    has no meaningful stdio encoding). Operator-supplied static headers
    are merged with the per-user bearer at connect time.
    """
    cfg: dict[str, Any] = {
        "type": row.get("transport") or "streamable-http",
        "url": row.get("url") or "",
    }
    headers_raw = row.get("headers")
    parsed_headers: dict[str, str] = {}
    if isinstance(headers_raw, dict):
        for k, v in headers_raw.items():
            if isinstance(k, str) and isinstance(v, str):
                parsed_headers[k] = v
    elif isinstance(headers_raw, str) and headers_raw:
        try:
            decoded = json.loads(headers_raw)
        except (json.JSONDecodeError, TypeError):
            decoded = {}
        if isinstance(decoded, dict):
            for k, v in decoded.items():
                if isinstance(k, str) and isinstance(v, str):
                    parsed_headers[k] = v
    cfg["headers"] = parsed_headers
    return cfg


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _db_servers_to_config(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Convert mcp_servers DB rows to the config dict format.

    Skips pool-backed rows (``auth_type='oauth_user'`` / ``'oauth_obo'``):
    they need per-user bearer tokens fetched at dispatch time, so
    auto-connecting them at startup with empty headers fails handshake and
    trips the circuit breaker. The per-user pool brings them online lazily —
    on consent for oauth_user, on first dispatch for oauth_obo.
    """
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if is_user_scoped_auth(row.get("auth_type")):
            continue
        name = row["name"]
        cfg: dict[str, Any] = {"type": row["transport"]}
        if row["transport"] == "stdio":
            cfg["command"] = row.get("command", "")
            try:
                cfg["args"] = json.loads(row.get("args", "[]"))
            except (json.JSONDecodeError, TypeError):
                cfg["args"] = []
            try:
                cfg["env"] = json.loads(row.get("env", "{}"))
            except (json.JSONDecodeError, TypeError):
                cfg["env"] = {}
        else:
            cfg["url"] = row.get("url", "")
            try:
                cfg["headers"] = json.loads(row.get("headers", "{}"))
            except (json.JSONDecodeError, TypeError):
                cfg["headers"] = {}
        result[name] = cfg
    return result


def load_mcp_config(
    config_path: str | None = None,
    storage: Any = None,
) -> dict[str, dict[str, Any]]:
    """Load MCP server configurations.

    Sources (first match wins):

    1. DB ``mcp_servers`` table (if *storage* provided and has enabled rows).
    2. Explicit *config_path* (standard MCP JSON format).
    3. ``[mcp.servers.*]`` sections in ``config.toml``.

    Returns an empty dict if nothing is configured.
    """
    # 1. Database
    if storage is not None:
        try:
            rows = storage.list_mcp_servers(enabled_only=True)
            if rows:
                servers = _db_servers_to_config(rows)
                log.info("Loaded MCP config from database (%d server(s))", len(servers))
                return servers
        except Exception:
            log.debug("DB MCP config lookup failed (table may not exist yet)", exc_info=True)

    # 2. Explicit JSON file
    if config_path:
        path = Path(config_path).expanduser()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                servers = data.get("mcpServers", {})
                if isinstance(servers, dict) and servers:
                    log.info("Loaded MCP config from %s (%d server(s))", path, len(servers))
                    return servers
            except Exception:
                log.warning("Failed to parse MCP config file: %s", path, exc_info=True)
        else:
            log.warning("MCP config file not found: %s", path)

    # 3. TOML config
    mcp_section = load_config("mcp")
    servers_section = mcp_section.get("servers", {})

    # If TOML has [mcp] config_path, try that JSON file
    toml_config_path = mcp_section.get("config_path")
    if toml_config_path and not config_path:
        return load_mcp_config(toml_config_path)

    if isinstance(servers_section, dict) and servers_section:
        log.info("Loaded MCP config from config.toml (%d server(s))", len(servers_section))
        return servers_section

    return {}


def create_mcp_client(
    config_path: str | None = None,
    *,
    storage: Any = None,
) -> MCPClientManager | None:
    """Create and start an MCP client manager.

    Returns *None* if no servers are configured.
    """
    # Check DB first to know which servers are DB-managed
    db_names: set[str] = set()
    oauth_user_names: set[str] = set()
    obo_names: set[str] = set()
    if storage is not None:
        try:
            rows = storage.list_mcp_servers(enabled_only=True)
            if rows:
                db_names = {r["name"] for r in rows}
                # Cache pool-backed names so per-turn callers (web_search
                # backend resolution) can answer auth_type without SQL.
                oauth_user_names = {r["name"] for r in rows if r.get("auth_type") == "oauth_user"}
                obo_names = {r["name"] for r in rows if r.get("auth_type") == "oauth_obo"}
        except Exception:
            log.warning("Failed to load DB-managed MCP servers", exc_info=True)

    servers = load_mcp_config(config_path, storage=storage)
    if not servers:
        return None

    mgr = MCPClientManager(servers)
    # Mark DB-sourced servers so reconcile_sync won't remove config-file servers
    mgr._db_managed = {name for name in servers if name in db_names}
    mgr._oauth_user_server_names = oauth_user_names
    mgr._obo_server_names = obo_names
    mgr.start()
    return mgr
