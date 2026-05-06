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
import json
import random
import threading
import time
import urllib.parse
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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
    parse_www_authenticate_error,
    parse_www_authenticate_scope,
)
from turnstone.core.mcp_oauth import (
    TokenLookupResult,
    emit_insufficient_scope_audit,
    get_user_access_token_classified,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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


# Defensive cap on the number of scopes we report in
# ``mcp_insufficient_scope`` audit/error payloads. Real ASes return
# single-digit scope counts; the cap stops a malicious upstream from
# bloating either surface via a thousand-token ``scope=`` value.
_MAX_INSUFFICIENT_SCOPE_REPORTED = 32

# Defensive cap on the number of tools we accept from any single MCP
# server's ``tools/list`` response. Real servers expose at most a few
# dozen tools; a misconfigured or hostile upstream returning thousands
# would amplify both memory (one OpenAI tool dict per entry) and
# downstream BM25 reindex cost. Mirrors the ``_MAX_ERROR_LEN`` /
# ``_MAX_INSUFFICIENT_SCOPE_REPORTED`` defensive ceilings: we truncate
# rather than reject so partial visibility beats zero visibility, and
# emit a warning so operators can investigate.
_MAX_TOOLS_PER_SERVER = 1000


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
    stack: AsyncExitStack | None = None
    streams: tuple[Any, Any] | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
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
    stack: AsyncExitStack | None = None
    streams: tuple[Any, Any] | None = None
    # Catalog state — populated lazily once per-user discovery wires
    # in; left ``None`` here so 200-entry pools don't retain 600 empty
    # list objects.
    tools: list[dict[str, Any]] | None = None
    resources: list[dict[str, Any]] | None = None
    prompts: list[dict[str, Any]] | None = None
    last_used: float = 0.0
    in_flight: int = 0
    auth_capture: _AuthCapture = field(default_factory=_AuthCapture)
    # Set by the response hook when the carrier captures a 4xx; awaited
    # by ``_dispatch_pool_with_entry``'s race against ``call_tool``.
    # Must be allocated on the mcp-loop (per :class:`asyncio.Event`'s
    # loop-binding contract); ``_ensure_pool_entry`` runs on the loop
    # so the dataclass default_factory is safe.
    auth_fired_event: asyncio.Event = field(default_factory=asyncio.Event)


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
        # session/stack/streams/catalog/capability flags for one name-keyed
        # connection.  The upcoming per-user pool integration introduces a
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
        self._resource_listeners: list[Callable[[], None]] = []
        self._resource_listeners_lock = threading.Lock()

        # Merged prompt catalog
        self._prompts: list[dict[str, Any]] = []
        self._prompt_map: dict[str, tuple[str, str]] = {}  # prefixed → (server, original)
        self._prompt_listeners: list[Callable[[], None]] = []
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

        # Notification debounce (per-server)
        self._last_notification_refresh: dict[str, float] = {}

        # Per-(user, server) state for auth_type=oauth_user. Loop-bound:
        # mutated only on the mcp-loop. Sync threads interact via
        # ``asyncio.run_coroutine_threadsafe``.
        self._user_pool_entries: dict[tuple[str, str], PoolEntryState] = {}
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
        # Resource/prompt mirrors are deferred to Phase 7b — Phase 7
        # only lights up the tool path needed for invariant 8.
        self._user_tool_map: dict[str, dict[str, tuple[str, str]]] = {}
        # Per-user merged tool list (one snapshot per user_id), updated
        # atomically alongside ``_user_tool_map`` in
        # ``_rebuild_user_tool_map``. ``get_tools(user_id=...)`` reads
        # this via a single dict-get (atomic under GIL) so sync-thread
        # callers (ChatSession) never iterate ``_user_pool_entries``
        # concurrently with the mcp-loop's mutations of the same dict
        # (insert in ``_ensure_pool_entry`` / pop in
        # ``_close_pool_entry_if_idle`` / ``_evict_session``).
        self._user_tools: dict[str, list[dict[str, Any]]] = {}

        # Notification debounce for pool sessions, keyed ``(user_id, server)``.
        # Mirrors ``_last_notification_refresh`` (static) but per-pool-key
        # so a noisy server in one user's pool doesn't suppress a refresh
        # in another user's pool of the same server.
        self._last_pool_notification_refresh: dict[tuple[str, str], float] = {}

        # Pool tuning (read at construction; falls back to defaults).
        mcp_cfg = load_config("mcp")
        self._user_pool_idle_ttl_s = float(mcp_cfg.get("user_session_idle_ttl_seconds", 600))
        self._user_pool_lru_max = int(mcp_cfg.get("user_session_lru_max", 200))

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

        # Idle-eviction task handle. Scheduled lazily on the mcp-loop the
        # first time a pool entry is created (start() runs before pool
        # rows exist, so deferring keeps the task count at zero in
        # static-only deployments).
        self._user_pool_eviction_task: asyncio.Task[None] | None = None

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
            except Exception as exc:
                log.warning("Failed to connect MCP server '%s'", name, exc_info=True)
                self._set_error(name, f"{type(exc).__name__}: {exc}")
                self._cb_record_failure(name)

        self._connected.set()

    _CONNECT_TIMEOUT = 30  # seconds — prevents hung connections on broken remotes
    _TCP_PROBE_TIMEOUT = 5  # seconds — fast TCP pre-flight for HTTP transports

    # Circuit breaker constants
    _CB_FAILURE_THRESHOLD = 3
    _CB_BASE_COOLDOWN = 30.0  # seconds
    _CB_MAX_COOLDOWN = 300.0  # 5 minutes

    # Notification debounce
    _NOTIFICATION_DEBOUNCE = 5.0  # seconds between refreshes per server

    # Pool eviction loop tick interval (per-iteration sleep, seconds).
    _POOL_EVICTION_TICK_S = 30.0

    # Best-effort lock acquire timeout for the eviction pass; eviction must
    # never block on a contested per-key lock so the next tick retries.
    _POOL_EVICTION_LOCK_ACQUIRE_TIMEOUT_S = 0.05

    # -- circuit breaker (per-server) -----------------------------------------

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
            cooldown = min(self._CB_BASE_COOLDOWN * (2**trips), self._CB_MAX_COOLDOWN)
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
        """Close MCP transport streams before stack teardown.

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

    @staticmethod
    async def _safe_close_stack(stack: AsyncExitStack) -> None:
        """Close an AsyncExitStack, suppressing errors from broken anyio scopes.

        Called from exception handlers — must not raise, otherwise cleanup
        errors could mask the original exception.  CancelledError is caught
        explicitly because it is the primary failure mode (stray cancel from
        broken anyio scope) and is BaseException, not Exception.

        ``BaseExceptionGroup`` is also caught: anyio's TaskGroup wraps any
        unhandled task exception (e.g., the SDK's
        ``HTTPStatusError`` raised inside ``post_writer``'s
        ``tg.start_soon(handle_request_async)`` after a 401/403)
        into a ``BaseExceptionGroup`` on ``__aexit__``. Without this catch
        the auth-retry path's eager teardown would propagate the SDK's
        own collected fallout.

        The 5s timeout uses ``asyncio.timeout`` (NOT ``asyncio.wait_for``)
        because Python 3.11's ``asyncio.wait_for`` wraps its inner
        coroutine in a fresh :class:`asyncio.Task`. When that fresh
        task runs ``stack.aclose()``, it tries to exit anyio cancel
        scopes that were entered in the CALLING task — anyio rejects
        the cross-task scope-exit with
        ``RuntimeError('Attempted to exit cancel scope in a different
        task than it was entered in')`` and the aclose fails.
        ``asyncio.timeout`` runs the inner code in the current task
        (matches Python 3.12+'s ``wait_for`` rewrite), preserving
        scope-exit identity. The 5s bound stays as protection against
        ``aclose()`` hanging on a broken stack (a never-completing
        anyio task during teardown).
        """
        try:
            async with asyncio.timeout(5):
                await stack.aclose()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup):
            log.debug("Error closing AsyncExitStack; ignoring", exc_info=True)

    async def _safe_teardown_on_connect_failure(
        self, key: str | tuple[str, str], stack: AsyncExitStack
    ) -> None:
        """Pre-close streams + close the stack on a connect-time failure.

        Shared by ``_connect_one`` (static) and ``_connect_one_pool``
        (per-(user, server)) — both raise from inside the stream-enter /
        session-init try blocks, and both must drain the anyio
        zero-buffer ``send()`` calls before closing the stack to avoid
        the SDK #2147 CPU busy-loop.
        """
        await self._pre_close_streams(key)
        await self._safe_close_stack(stack)

    async def _connect_one(self, name: str, cfg: dict[str, Any]) -> None:
        """Connect to a single MCP server and discover its tools."""
        if "__" in name:
            log.error("MCP server name '%s' contains '__' (reserved delimiter), skipping", name)
            return

        # Operate on a single state object throughout: get-or-create up front
        # so the stale-entry guard and the post-handshake field assignments
        # touch the same instance (PR #296 invariant 5: identity stability).
        state = self._ensure_static_state(name)

        # Guard: tear down stale session/stack so we don't leak.  Checks both
        # session and stack because transport errors in the sync dispatch
        # methods evict the session but leave the stack behind.  On a brand
        # new entry both fields are None, so this branch is skipped.
        if state.session is not None or state.stack is not None:
            state.session = None
            await self._pre_close_streams(name)
            old_stack = state.stack
            state.stack = None
            if old_stack is not None:
                await self._safe_close_stack(old_stack)

        # Per-server exit stack for clean per-server lifecycle management
        stack = AsyncExitStack()
        await stack.__aenter__()

        transport = cfg.get("type", "stdio")
        try:
            if transport in ("http", "streamable-http") or "url" in cfg:
                # Pre-flight TCP check: fail fast before entering the anyio
                # task group in streamablehttp_client.  An immediate connect
                # failure (ECONNREFUSED) inside the anyio context causes a
                # CancelledError that escapes asyncio.wait_for and leaves
                # orphaned cancel-scope tasks spinning at 100% CPU.
                await self._tcp_probe(name, cfg["url"])

                read, write, _ = await asyncio.wait_for(
                    stack.enter_async_context(
                        streamablehttp_client(url=cfg["url"], headers=cfg.get("headers"))
                    ),
                    timeout=self._CONNECT_TIMEOUT,
                )
                # Stash stream refs so _pre_close_streams can unblock anyio
                # transport tasks before the cancel scope fires (SDK #2147).
                state.streams = (read, write)
            else:
                # Default: stdio transport
                command = cfg.get("command", "")
                if not command:
                    log.warning("MCP server '%s' has no command configured", name)
                    await stack.aclose()
                    return
                from turnstone.core.env import scrubbed_env

                env = scrubbed_env(extra=cfg.get("env", {}))
                params = StdioServerParameters(
                    command=command,
                    args=cfg.get("args", []),
                    env=env,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
                state.streams = (read, write)
        except asyncio.CancelledError:
            # Stray CancelledError from broken anyio cancel scope -- treat as
            # connection failure.  But if the task is genuinely being cancelled
            # (shutdown), re-raise so we don't block teardown.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                await self._safe_teardown_on_connect_failure(name, stack)
                raise
            log.warning("MCP server '%s' connection failed (anyio cancel)", name)
            await self._safe_teardown_on_connect_failure(name, stack)
            raise TimeoutError(f"Connection failed for '{name}'") from None
        except TimeoutError:
            log.warning(
                "MCP server '%s' connection timed out after %ds", name, self._CONNECT_TIMEOUT
            )
            await self._safe_teardown_on_connect_failure(name, stack)
            raise TimeoutError(f"Connection timed out after {self._CONNECT_TIMEOUT}s") from None
        except Exception:
            await self._safe_teardown_on_connect_failure(name, stack)
            raise

        # Register notification handler — dispatches tool, resource, and
        # prompt list-change notifications to the appropriate refresh method.
        async def _on_notification(
            msg: Any,  # RequestResponder | ServerNotification | Exception
        ) -> None:
            if not isinstance(msg, mcp_types.ServerNotification):
                return
            root = msg.root

            # Debounce: skip if we refreshed this server very recently
            now = time.monotonic()
            last = self._last_notification_refresh.get(name, 0.0)
            if now - last < self._NOTIFICATION_DEBOUNCE:
                log.debug(
                    "Debouncing notification from '%s' (%.1fs since last refresh)",
                    name,
                    now - last,
                )
                return

            try:
                if isinstance(root, mcp_types.ToolListChangedNotification):
                    log.info("Received tools/list_changed from '%s'", name)
                    self._last_notification_refresh[name] = now
                    await self._refresh_server_tools(name)
                elif isinstance(root, mcp_types.ResourceListChangedNotification):
                    log.info("Received resources/list_changed from '%s'", name)
                    self._last_notification_refresh[name] = now
                    await self._refresh_server_resources(name)
                elif isinstance(root, mcp_types.PromptListChangedNotification):
                    log.info("Received prompts/list_changed from '%s'", name)
                    self._last_notification_refresh[name] = now
                    await self._refresh_server_prompts(name)
                self._last_error.pop(name, None)
            except Exception as exc:
                log.warning("Refresh after notification failed for '%s'", name, exc_info=True)
                self._set_error(name, f"Refresh failed: {exc}")

        try:
            session = await stack.enter_async_context(
                ClientSession(read, write, message_handler=_on_notification)  # type: ignore[arg-type]
            )
        except Exception:
            await self._safe_teardown_on_connect_failure(name, stack)
            raise

        state.stack = stack
        try:
            await asyncio.wait_for(session.initialize(), timeout=self._CONNECT_TIMEOUT)
        except asyncio.CancelledError:
            state.stack = None
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                await self._safe_teardown_on_connect_failure(name, stack)
                raise
            await self._safe_teardown_on_connect_failure(name, stack)
            raise TimeoutError(f"MCP handshake failed for '{name}'") from None
        except TimeoutError:
            state.stack = None
            await self._safe_teardown_on_connect_failure(name, stack)
            raise TimeoutError(f"MCP handshake timed out after {self._CONNECT_TIMEOUT}s") from None
        except Exception:
            state.stack = None
            await self._safe_teardown_on_connect_failure(name, stack)
            raise
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

        # Discover tools
        result = await session.list_tools()
        capped = _cap_server_tools(name, result.tools)
        server_tools: list[dict[str, Any]] = [_mcp_to_openai(name, tool) for tool in capped]

        state.tools = server_tools
        self._rebuild_tools()

        # Discover resources
        resource_count = 0
        if resources_cap is not None:
            server_resources: list[dict[str, Any]] = []
            res_result = await session.list_resources()
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
            tmpl_result = await session.list_resource_templates()
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
            prompt_result = await session.list_prompts()
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
        * Tool catalog discovery runs after ``initialize()``; the
          notification handler is bound to ``(user_id, server_name)`` so
          push-driven ``tools/list_changed`` updates only refresh the
          owning user's catalog (the static refresher must NEVER fire
          from a pool session — it would clobber static-path state).
          Resource / prompt discovery is deferred to Phase 7b.

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

        # Tear down any stale session/stack the same way ``_connect_one``
        # does (cf. PR #296 invariant 5 for the static path).
        if entry.session is not None or entry.stack is not None:
            entry.session = None
            await self._pre_close_streams(key)
            old_stack = entry.stack
            entry.stack = None
            if old_stack is not None:
                await self._safe_close_stack(old_stack)

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

        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            await self._tcp_probe(key, url)
            read, write, _ = await asyncio.wait_for(
                stack.enter_async_context(streamablehttp_client(**client_kwargs)),
                timeout=self._CONNECT_TIMEOUT,
            )
            entry.streams = (read, write)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                await self._safe_teardown_on_connect_failure(key, stack)
                raise
            log.warning(
                "MCP pool connect failed (anyio cancel) user=%s server=%s", user_id, server_name
            )
            await self._safe_teardown_on_connect_failure(key, stack)
            raise TimeoutError(f"Pool connection failed for '{server_name}'") from None
        except TimeoutError:
            log.warning(
                "MCP pool connect timed out after %ds user=%s server=%s",
                self._CONNECT_TIMEOUT,
                user_id,
                server_name,
            )
            await self._safe_teardown_on_connect_failure(key, stack)
            raise TimeoutError(
                f"Pool connection timed out after {self._CONNECT_TIMEOUT}s"
            ) from None
        except Exception:
            await self._safe_teardown_on_connect_failure(key, stack)
            raise

        # Pool-scoped notification handler — bound to ``(user_id,
        # server_name)`` via closure so a push-driven
        # ``tools/list_changed`` only refreshes THIS user's catalog,
        # never the static path's. Calling the static
        # ``_refresh_server_tools(server_name)`` from a pool session
        # would mutate ``_static_servers[server_name].tools`` and
        # ``_tool_map`` — breaking invariant 1 (static-path
        # byte-identical) and broadcasting one user's tool view to all
        # other sessions. Phase 7 ToolListChangedNotification only;
        # resource / prompt branches log + skip until Phase 7b adds
        # ``_refresh_pool_server_resources`` / ``_refresh_pool_server_prompts``.
        async def _on_pool_notification(
            msg: Any,  # RequestResponder | ServerNotification | Exception
        ) -> None:
            if not isinstance(msg, mcp_types.ServerNotification):
                return
            root = msg.root
            now = time.monotonic()
            last = self._last_pool_notification_refresh.get(key, 0.0)
            if now - last < self._NOTIFICATION_DEBOUNCE:
                log.debug(
                    "Debouncing pool notification user=%s server=%s (%.1fs since last refresh)",
                    user_id,
                    server_name,
                    now - last,
                )
                return
            try:
                if isinstance(root, mcp_types.ToolListChangedNotification):
                    log.info(
                        "Received tools/list_changed from pool user=%s server=%s",
                        user_id,
                        server_name,
                    )
                    self._last_pool_notification_refresh[key] = now
                    await self._refresh_pool_server_tools(key)
                elif isinstance(root, mcp_types.ResourceListChangedNotification):
                    log.debug(
                        "pool resources/list_changed (deferred to Phase 7b) user=%s server=%s",
                        user_id,
                        server_name,
                    )
                elif isinstance(root, mcp_types.PromptListChangedNotification):
                    log.debug(
                        "pool prompts/list_changed (deferred to Phase 7b) user=%s server=%s",
                        user_id,
                        server_name,
                    )
            except Exception:
                log.warning(
                    "Pool refresh after notification failed user=%s server=%s",
                    user_id,
                    server_name,
                    exc_info=True,
                )

        try:
            session = await stack.enter_async_context(
                ClientSession(read, write, message_handler=_on_pool_notification)  # type: ignore[arg-type]
            )
        except Exception:
            await self._safe_teardown_on_connect_failure(key, stack)
            raise

        entry.stack = stack
        try:
            await asyncio.wait_for(session.initialize(), timeout=self._CONNECT_TIMEOUT)
        except asyncio.CancelledError:
            entry.stack = None
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                await self._safe_teardown_on_connect_failure(key, stack)
                raise
            await self._safe_teardown_on_connect_failure(key, stack)
            raise TimeoutError(f"Pool handshake failed for '{server_name}'") from None
        except TimeoutError:
            entry.stack = None
            await self._safe_teardown_on_connect_failure(key, stack)
            raise TimeoutError(f"Pool handshake timed out after {self._CONNECT_TIMEOUT}s") from None
        except Exception:
            entry.stack = None
            await self._safe_teardown_on_connect_failure(key, stack)
            raise

        # Discover this user's tool catalog. R6 verified: a 401 here
        # propagates through anyio TaskGroup unwinding (raises an
        # ``ExceptionGroup`` from the surrounding ``streamablehttp_client``
        # context) — plain ``await`` under ``asyncio.timeout`` is
        # sufficient. The carrier-race shape used by
        # ``_dispatch_pool_with_entry`` defends a different scenario
        # (reused-session 401 from inside a SECOND dispatch) that
        # doesn't apply to first-connect discovery. Resource / prompt
        # discovery deferred to Phase 7b.
        #
        # Why ``asyncio.timeout``, not ``asyncio.wait_for``: per
        # ``feedback_asyncio_timeout_vs_wait_for.md`` and the f6a3b66
        # fix, Python 3.11's ``asyncio.wait_for`` wraps the inner
        # coroutine in a fresh task. When the SDK's ``streamablehttp_client``
        # TaskGroup unwinds (e.g. on a 401), ``aclose`` on the surrounding
        # anyio scope runs from a different task than entered it →
        # ``RuntimeError("Attempted to exit cancel scope in a different
        # task")``. ``asyncio.timeout`` runs the inner coroutine in the
        # current task and is the safe shape for any await that may
        # traverse anyio cleanup.
        try:
            async with asyncio.timeout(self._CONNECT_TIMEOUT):
                tools_result = await session.list_tools()
        except asyncio.CancelledError:
            entry.stack = None
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                await self._safe_teardown_on_connect_failure(key, stack)
                raise
            await self._safe_teardown_on_connect_failure(key, stack)
            raise TimeoutError(f"Pool discovery failed for '{server_name}'") from None
        except TimeoutError:
            entry.stack = None
            await self._safe_teardown_on_connect_failure(key, stack)
            raise TimeoutError(f"Pool discovery timed out after {self._CONNECT_TIMEOUT}s") from None
        except Exception:
            entry.stack = None
            await self._safe_teardown_on_connect_failure(key, stack)
            raise

        capped_tools = _cap_server_tools(server_name, tools_result.tools)
        entry.tools = [_mcp_to_openai(server_name, tool) for tool in capped_tools]
        # Publish session readiness BEFORE catalog visibility.
        # ``_rebuild_user_tool_map`` makes ``is_mcp_tool(name, user_id=U)``
        # return True for the discovered names; if catalog visibility
        # preceded ``entry.session`` assignment, a sync-thread reader
        # racing with this coroutine could observe a tool whose backing
        # entry has ``session=None``. Defence-in-depth — dispatch
        # re-fetches its own token through ``get_user_access_token_classified``
        # and lazy-reconnects on session=None — but ordering catches the
        # race at the source rather than relying on the dispatch-time
        # recovery path.
        entry.session = session
        entry.last_used = time.monotonic()
        self._user_pool_last_used[key] = entry.last_used

        # Loop-only mutation; sync-thread readers observe the new tool
        # list atomically via the per-user dict-get on ``_user_tools``.
        self._rebuild_user_tool_map(user_id)
        # Wake user-keyed AND admin (None) listeners; per-user fan-out
        # ensures another user's session never observes this user's
        # tool change.
        self._notify_user_tool_listeners(user_id)
        return entry

    # -- pool eviction --------------------------------------------------------

    async def _user_pool_eviction_loop(self) -> None:
        """Long-running coroutine — periodically evicts idle pool entries.

        Note: the loop wakes on a fixed tick rather than a condition
        variable. Event-driven evictions would be more efficient on
        large idle pools but add complexity (tracking per-entry
        deadlines + a wakeup ``asyncio.Event``); at the design cap of
        200 entries the unconditional 30 s wake is negligible.
        """
        while True:
            try:
                await asyncio.sleep(self._POOL_EVICTION_TICK_S)
                await self._evict_idle_pool_entries()
            except asyncio.CancelledError:
                return
            except Exception:
                log.warning("MCP pool eviction iteration failed", exc_info=True)

    async def _evict_idle_pool_entries(self) -> None:
        """Evict pool entries past the idle TTL or above the LRU cap.

        Skips any key whose ``open_lock`` is currently held or whose
        ``in_flight`` counter is non-zero — eviction never blocks on a
        contested lock or an active dispatch; the next tick retries.
        """
        if not self._user_pool_entries:
            return
        now = time.monotonic()
        ttl = self._user_pool_idle_ttl_s

        # First pass: TTL-based eviction. Run closes in parallel so a tick
        # that needs to evict many entries doesn't block on serial teardowns.
        ttl_targets: list[tuple[str, str]] = []
        for key, entry in list(self._user_pool_entries.items()):
            last = self._user_pool_last_used.get(key, entry.last_used)
            if (now - last) >= ttl:
                ttl_targets.append(key)
        if ttl_targets:
            await asyncio.gather(
                *(self._close_pool_entry_if_idle(k) for k in ttl_targets),
                return_exceptions=True,
            )

        # Second pass: LRU cap. Iterate the canonical entry map (not the
        # last_used view) so brand-new entries that were created via
        # ``_ensure_pool_entry`` but haven't dispatched yet are still
        # eviction-eligible.
        if len(self._user_pool_entries) <= self._user_pool_lru_max:
            return
        ordered = sorted(
            self._user_pool_entries.items(),
            key=lambda kv: self._user_pool_last_used.get(kv[0], kv[1].last_used),
        )
        # Compute the eviction batch up front; we re-check the cap after
        # each close (in-flight skips can leave us still over).
        for key, _entry in ordered:
            if len(self._user_pool_entries) <= self._user_pool_lru_max:
                break
            await self._close_pool_entry_if_idle(key)

    async def _close_pool_entry_if_idle(self, key: tuple[str, str]) -> None:
        """Close ``key`` iff its open_lock is uncontested AND in_flight==0.

        Best-effort: a contested lock or an active dispatch causes the
        function to return without mutation; the next eviction tick
        retries.
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
            entry.session = None
            await self._pre_close_streams(key)
            stack = entry.stack
            entry.stack = None
            if stack is not None:
                await self._safe_close_stack(stack)
            self._user_pool_entries.pop(key, None)
            self._user_pool_last_used.pop(key, None)
            # Mirror ``_evict_session``'s catalog cleanup: dropping the
            # entry without rebuilding ``_user_tool_map`` would leave
            # ``is_mcp_tool`` returning True for tools whose backing
            # pool is gone, and ChatSession's ``_tools`` would never
            # rebuild because no listener fires.
            self._last_pool_notification_refresh.pop(key, None)
            user_id, _server_name = key
            self._rebuild_user_tool_map(user_id)
            self._notify_user_tool_listeners(user_id)
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
        if isinstance(exc, McpError):
            return "protocol"
        if isinstance(exc, BrokenPipeError | ConnectionResetError | EOFError | TimeoutError):
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
        for (uid, _server_name), entry in self._user_pool_entries.items():
            if uid != user_id or entry.tools is None:
                continue
            for tool in entry.tools:
                prefixed: str = tool["function"]["name"]
                # Extract original name from the mcp__server__original pattern.
                original = prefixed.split("__", 2)[2] if prefixed.count("__") >= 2 else prefixed
                new_map[prefixed] = (_server_name, original)
                new_tools.append(tool)
        if new_map:
            self._user_tool_map[user_id] = new_map
            self._user_tools[user_id] = new_tools
        else:
            self._user_tool_map.pop(user_id, None)
            self._user_tools.pop(user_id, None)

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

        result = await session.list_tools()
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

        MUST run on the mcp-loop. Caller does not need to hold
        ``open_lock`` — ``_refresh_pool_server_tools`` is invoked from
        the pool session's notification handler, which already runs in
        the SDK's receive task on the loop.
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
        result = await session.list_tools()
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

    async def _refresh_server(self, name: str) -> tuple[list[str], list[str]]:
        """Re-fetch tools, resources, and prompts for one server.

        Returns ``(added_tools, removed_tools)`` names (tool diff only,
        for backward compatibility with ``/mcp refresh`` output).
        """
        tool_diff, _, _ = await asyncio.gather(
            self._refresh_server_tools(name),
            self._refresh_server_resources(name),
            self._refresh_server_prompts(name),
        )
        added, removed = tool_diff
        self._last_error.pop(name, None)
        return added, removed

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
                    # Attempt reconnect
                    cfg = self._server_configs.get(name)
                    if cfg:
                        log.info("Reconnecting MCP server '%s'", name)
                        await self._connect_one(name, cfg)
                        self._cb_record_success(name)
                        post = self._static_servers.get(name)
                        new_names = (
                            [t["function"]["name"] for t in post.tools] if post is not None else []
                        )
                        results[name] = (new_names, [])
                    continue
                added, removed = await self._refresh_server(name)
                self._cb_record_success(name)
                results[name] = (added, removed)
            except Exception as exc:
                log.warning("Refresh failed for MCP server '%s'", name, exc_info=True)
                self._set_error(name, f"Refresh failed: {exc}")
                results[name] = ([], [])

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

        server_resources: list[dict[str, Any]] = []
        res_result = await session.list_resources()
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
        tmpl_result = await session.list_resource_templates()
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

        server_prompts: list[dict[str, Any]] = []
        prompt_result = await session.list_prompts()
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

    def add_resource_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the resource list changes."""
        with self._resource_listeners_lock:
            self._resource_listeners.append(callback)

    def remove_resource_listener(self, callback: Callable[[], None]) -> None:
        """Unregister a resource-change callback."""
        with self._resource_listeners_lock, contextlib.suppress(ValueError):
            self._resource_listeners.remove(callback)

    def _notify_resource_listeners(self) -> None:
        """Invoke all registered resource-change listeners."""
        with self._resource_listeners_lock:
            listeners = list(self._resource_listeners)
        for cb in listeners:
            try:
                cb()
            except Exception:
                log.warning("Resource-change listener raised", exc_info=True)

    def add_prompt_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the prompt list changes."""
        with self._prompt_listeners_lock:
            self._prompt_listeners.append(callback)

    def remove_prompt_listener(self, callback: Callable[[], None]) -> None:
        """Unregister a prompt-change callback."""
        with self._prompt_listeners_lock, contextlib.suppress(ValueError):
            self._prompt_listeners.remove(callback)

    def _notify_prompt_listeners(self) -> None:
        """Invoke all registered prompt-change listeners."""
        with self._prompt_listeners_lock:
            listeners = list(self._prompt_listeners)
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
                for key in list(self._user_pool_entries):
                    await self._pre_close_streams(key)
                    entry = self._user_pool_entries.get(key)
                    if entry is not None and entry.stack is not None:
                        await self._safe_close_stack(entry.stack)
                self._user_pool_entries.clear()
                self._user_pool_last_used.clear()
                self._user_pool_locks.clear()

            future = asyncio.run_coroutine_threadsafe(_close_all_pool(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                log.debug("Error closing MCP pool sessions", exc_info=True)

        # Close all per-server stacks (transports + sessions)
        if self._loop and self._static_servers:

            async def _close_all_stacks() -> None:
                # Pre-close streams to prevent anyio CPU busy-loop during teardown
                for srv_name in list(self._static_servers):
                    await self._pre_close_streams(srv_name)
                for srv_state in self._static_servers.values():
                    if srv_state.stack is not None:
                        await self._safe_close_stack(srv_state.stack)

            future = asyncio.run_coroutine_threadsafe(_close_all_stacks(), self._loop)
            try:
                future.result(timeout=10)
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

        # Clear all state
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
        # Pool state already cleared above when the loop was alive; this
        # makes shutdown idempotent if the loop exited before pool state
        # could be wound down (e.g., manager constructed but never started).
        self._user_pool_entries.clear()
        self._user_pool_last_used.clear()
        self._user_pool_locks.clear()
        self._user_pool_eviction_task = None

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

    def reconnect_sync(self, name: str, timeout: int = 30) -> dict[str, Any]:
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
            state = self._static_servers.get(name)
            if state is not None:
                state.session = None
                await self._pre_close_streams(name)
                old_stack = state.stack
                state.stack = None
                if old_stack is not None:
                    await self._safe_close_stack(old_stack)
            try:
                await self._connect_one(name, cfg)
            except Exception:
                # Connect failed mid-reconnect — drop the stale per-server
                # catalog so the merged tool/resource/prompt maps don't keep
                # advertising entries with no live session behind them.
                fail_state = self._static_servers.get(name)
                if fail_state is not None:
                    fail_state.tools = []
                    fail_state.resources = []
                    fail_state.prompts = []
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

    def remove_server_sync(self, name: str, timeout: int = 15) -> bool:
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
                # Close session + transport via per-server stack
                state = self._static_servers.get(name)
                if state is not None:
                    state.session = None
                    await self._pre_close_streams(name)
                    stack = state.stack
                    state.stack = None
                    if stack is not None:
                        await self._safe_close_stack(stack)
                # Clean up per-server state (on the event loop thread)
                self._static_servers.pop(name, None)
                self._last_error.pop(name, None)
                self._last_notification_refresh.pop(name, None)
                self._cb_clear(name)
                # Rebuild merged state (serialized with notification handlers)
                self._rebuild_tools()
                self._rebuild_resources()
                self._rebuild_prompts()

            future = asyncio.run_coroutine_threadsafe(_remove(), self._loop)
            try:
                future.result(timeout=timeout)
            except Exception:
                log.warning("Error removing MCP server '%s'", name, exc_info=True)
        else:
            # No event loop (tests / pre-start) — mutate directly
            self._static_servers.pop(name, None)
            self._last_error.pop(name, None)
            self._last_notification_refresh.pop(name, None)
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

    def get_server_status(self, name: str) -> dict[str, Any]:
        """Return live status for a single server, including config details."""
        state = self._static_servers.get(name)
        connected = state is not None and state.session is not None
        cfg = self._server_configs.get(name, {})
        transport = cfg.get("type", "stdio")
        cb_deadline = self._circuit_open_until.get(name)
        cb_open = cb_deadline is not None and time.monotonic() < cb_deadline
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
        }

    def get_all_server_status(self) -> dict[str, dict[str, Any]]:
        """Return live status for all configured servers."""
        result: dict[str, dict[str, Any]] = {}
        for name in list(self._server_configs):
            result[name] = self.get_server_status(name)
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

        # Refresh the in-memory oauth_user name cache from the rows we
        # just read — feeds :meth:`server_auth_type` so callers (e.g.
        # ``web_search.resolve_web_search_client``) avoid a per-turn SQL
        # roundtrip.
        self._oauth_user_server_names = {
            row["name"] for row in rows if row.get("auth_type") == "oauth_user"
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
        return {"added": added, "removed": removed, "updated": updated}

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

    def get_resources(self) -> list[dict[str, Any]]:
        """Return discovered MCP resources (shallow-copied dicts)."""
        return [dict(r) for r in self._resources]

    def get_prompts(self) -> list[dict[str, Any]]:
        """Return discovered MCP prompts (shallow-copied dicts)."""
        return [dict(p) for p in self._prompts]

    @property
    def resource_count(self) -> int:
        """Number of discovered resources (no allocation)."""
        return len(self._resources)

    @property
    def prompt_count(self) -> int:
        """Number of discovered prompts (no allocation)."""
        return len(self._prompts)

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

    def is_mcp_prompt(self, name: str) -> bool:
        """Check whether *name* is a known MCP prompt."""
        return name in self._prompt_map

    def server_auth_type(self, server_name: str) -> str | None:
        """Return ``'oauth_user'`` for pool-backed servers, else ``None``.

        In-memory accessor for the per-turn callers that need to
        distinguish pool-backed servers from static-path ones without a
        SQL roundtrip. ``None`` means "either static-path or unknown" —
        the boot-time / per-node web_search resolver only uses this as
        a defence-in-depth gate, so a missing-cache miss is safe (the
        outer ``is_mcp_tool`` check already proves the server is in
        ``_tool_map``, which by construction excludes oauth_user).
        Populated by ``reconcile_sync`` and ``create_mcp_client``.
        """
        return "oauth_user" if server_name in self._oauth_user_server_names else None

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

    def _cb_auto_reconnect(self, server_name: str) -> Any:
        """Attempt reconnection for a disconnected server during half-open probe.

        Returns the new session on success, or raises on failure.
        """
        cfg = self._server_configs.get(server_name)
        if not cfg or self._loop is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected")
        reconnect_future = asyncio.run_coroutine_threadsafe(
            self._connect_one(server_name, cfg), self._loop
        )
        try:
            reconnect_future.result(timeout=self._CONNECT_TIMEOUT)
        except concurrent.futures.TimeoutError:
            reconnect_future.cancel()
            self._cb_record_failure(server_name)
            raise RuntimeError(f"MCP server '{server_name}' reconnect timed out") from None
        except Exception as exc:
            self._cb_record_failure(server_name)
            raise RuntimeError(f"MCP server '{server_name}' reconnect failed: {exc}") from None
        state = self._static_servers.get(server_name)
        session = state.session if state is not None else None
        if session is None:
            self._cb_record_failure(server_name)
            raise RuntimeError(f"MCP server '{server_name}' reconnect produced no session")

        # Schedule catalog refresh on the loop without blocking the caller.
        # The reconnected session is valid for the imminent dispatch; catalog
        # drift will be reconciled on the loop in the background.
        def _schedule_refresh() -> None:
            try:
                asyncio.create_task(self._refresh_server(server_name))
            except Exception:
                log.warning(
                    "Catalog refresh after reconnect failed for '%s'",
                    server_name,
                    exc_info=True,
                )

        self._loop.call_soon_threadsafe(_schedule_refresh)
        return session

    def call_tool_sync(
        self,
        func_name: str,
        arguments: dict[str, Any],
        *,
        user_id: str | None = None,
        timeout: int = 120,
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
            session.call_tool(original_name, arguments), self._loop
        )
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._cb_record_failure(server_name)
            raise TimeoutError(f"MCP tool call timed out after {timeout}s") from None
        except Exception as exc:
            # Protocol errors (McpError) come from a healthy connection that
            # rejected the request — only transport errors trip the breaker.
            if not isinstance(exc, McpError):
                self._cb_record_failure(server_name)
            if isinstance(exc, BrokenPipeError | ConnectionResetError | EOFError):
                # Evict the session only — leave stack/streams behind so the
                # stale-session-and-stack guard in _connect_one cleans them up
                # on the next connect attempt.
                evict = self._static_servers.get(server_name)
                if evict is not None:
                    evict.session = None
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
        if row is None or row.get("auth_type") != "oauth_user":
            return None
        return server_name, original, row

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

    def _dispatch_pool_sync(
        self,
        *,
        user_id: str,
        server_name: str,
        original_name: str,
        arguments: dict[str, Any],
        server_row: dict[str, Any],
        timeout: int,
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
        """
        assert self._loop is not None
        start = time.monotonic()
        try:
            return self._run_pool_dispatch_attempt(
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
            return self._run_pool_dispatch_attempt(
                retry_count=1,
                timeout=remaining,
                original_timeout=timeout,
                user_id=user_id,
                server_name=server_name,
                original_name=original_name,
                arguments=arguments,
                server_row=server_row,
            )

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
        have ``auth_type='oauth_user'``.

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
        lookup: TokenLookupResult = await get_user_access_token_classified(
            app_state=self._app_state,
            user_id=user_id,
            server_name=server_name,
            force_refresh=retry_count > 0,
        )
        if lookup.kind == "missing":
            return _structured_error(
                code="mcp_consent_required",
                server=server_name,
                detail="No token for user. Consent flow required.",
            )
        if lookup.kind == "decrypt_failure":
            return _structured_error(
                code="mcp_token_undecryptable_key_unknown",
                server=server_name,
                detail=(
                    "Stored token cannot be decrypted by any installed encryption key. "
                    "Operator action required."
                ),
            )
        if lookup.kind == "refresh_failed":
            # ``mcp_server.oauth.token_revoked`` audit was already emitted
            # by ``get_user_access_token_classified`` when it deleted the
            # row; no second audit needed here.
            return _structured_error(
                code="mcp_consent_required",
                server=server_name,
                detail="Refresh token rejected. Re-consent required.",
            )
        # kind == "token"
        access_token = lookup.token or ""
        if not access_token:
            return _structured_error(
                code="mcp_consent_required",
                server=server_name,
                detail="No token for user. Consent flow required.",
            )

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
                return _structured_error(
                    code="mcp_consent_required",
                    server=server_name,
                    detail="Refreshed token still rejected. Re-consent required.",
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

    def _evict_session(self, key: tuple[str, str]) -> None:
        """Drop the cached session AND catalog on a pool entry.

        Stack/streams left for reconnect. Auth/transport branches both
        call this — the next connect's ``_connect_one_pool`` tears
        down the stale stack lazily via the stale-entry guard at the
        top of the method. Closing eagerly from here is incorrect
        under cancellation: ``stack.aclose()`` must run inside the
        same anyio scope it was entered in, which the next connect
        arranges.

        Catalog cleanup (Phase 7): clearing ``entry.tools`` here
        ensures an evicted-then-not-yet-reconnected entry contributes
        no stale entries to ``_user_tool_map``. Without this,
        ``is_mcp_tool(name, user_id=user_id)`` could return True for a
        name whose backing pool is gone, then ``_resolve_pool_target``
        would dispatch into a session-less entry and the next call
        would surface as a generic transport error instead of a
        clean reconnect path.
        """
        evict = self._user_pool_entries.get(key)
        if evict is not None:
            evict.session = None
            evict.tools = None
            # Prune the debounce stamp in lockstep with the entry — the
            # dict grows otherwise (slow leak) across (user, server)
            # churn. Mirrors the cleanup in ``_close_pool_entry_if_idle``.
            self._last_pool_notification_refresh.pop(key, None)
            user_id, _server_name = key
            self._rebuild_user_tool_map(user_id)
            # Wake the user's session so its merged tool list shrinks
            # back to static-only until the next connect populates the
            # entry. Admin (None) listeners also fire — operator
            # tooling tracking pool catalog state observes the drop.
            self._notify_user_tool_listeners(user_id)

    async def _handle_auth_403(
        self,
        *,
        user_id: str,
        server_name: str,
        server_row: dict[str, Any],
        capture: _AuthCapture,
    ) -> str:
        """Map a 403 + WWW-Authenticate into a structured error.

        ``error="insufficient_scope"`` becomes ``mcp_insufficient_scope``
        with the parsed ``scope=...`` set so the dashboard renderer
        can construct an authorize URL with the union of original +
        new scopes — re-consenting with the original scopes alone
        would loop because the AS would re-issue the same insufficient
        token. Other 403s become a generic ``mcp_tool_call_forbidden``
        with no retry; the user lacks permission and a step-up
        wouldn't help.
        """
        header = capture.www_authenticate or ""
        error_token = parse_www_authenticate_error(header)
        if error_token == "insufficient_scope":
            scopes = parse_www_authenticate_scope(header)
            scopes = scopes[:_MAX_INSUFFICIENT_SCOPE_REPORTED]
            await emit_insufficient_scope_audit(
                app_state=self._app_state,
                user_id=user_id,
                server_name=server_name,
                server_row=server_row,
                scopes=scopes,
            )
            return _structured_error(
                code="mcp_insufficient_scope",
                server=server_name,
                detail=("Tool requires elevated scopes. Re-consent flow with new scopes required."),
                scopes_required=list(scopes),
            )
        return _structured_error(
            code="mcp_tool_call_forbidden",
            server=server_name,
            detail="Tool call forbidden by upstream policy.",
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

        Lock held across ``call_tool`` because the entry's
        ``_AuthCapture`` is keyed off the httpx event hook; releasing
        the lock would let a concurrent same-(user, server) dispatch
        overwrite the carrier mid-flight and attribute one caller's
        auth failure to another. Holding the lock serialises
        same-(user, server) calls — acceptable because that contention
        scenario is rare in practice (ChatSession dispatches
        sequentially and per-(user, server) parallelism is not a
        production requirement).

        Reset of ``entry.auth_capture`` and ``entry.auth_fired_event``
        happens under the lock before ``call_tool`` — see
        :class:`PoolEntryState` for why the carrier is entry-owned.

        ``entry.in_flight`` accounting is preserved for the eviction
        interlock — it's belt-and-braces here since ``open_lock.locked()``
        already signals "do not evict", but the in-flight counter
        remains the source of truth for :meth:`_close_pool_entry_if_idle`.
        """
        async with entry.open_lock:
            entry.last_used = time.monotonic()
            self._user_pool_last_used[key] = entry.last_used
            entry.auth_capture.status = None
            entry.auth_capture.www_authenticate = None
            entry.auth_fired_event.clear()
            session = entry.session
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
                # Race ``call_tool`` against the carrier's fired event.
                # Without this race, an upstream 4xx on a REUSED session
                # never propagates back through ``call_tool``: the SDK's
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
                call_task = asyncio.create_task(session.call_tool(original_name, arguments))
                fired_task = asyncio.create_task(entry.auth_fired_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {call_task, fired_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    # Cancel-and-await both losers. Awaiting cancelled
                    # tasks here pins the broken session's streams
                    # against the auth_401 retry's
                    # ``_safe_close_stack`` teardown — without it the
                    # cancelled ``call_task`` could keep touching the
                    # SDK's stream state concurrently with the new
                    # ``_connect_one_pool``'s aclose.
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
                    result = call_task.result()
                else:
                    # Hook captured 4xx before call_tool returned. The
                    # SDK won't propagate the failure through
                    # call_tool, so eagerly tear down the session and
                    # raise a sentinel that the dispatcher's
                    # ``_classify_failure`` will resolve via the
                    # carrier (which holds the captured status).
                    raise _CarrierAuthSignal()
            finally:
                entry.in_flight -= 1
        return _decode_tool_result(result)

    # -- resource read -------------------------------------------------------

    def _match_template(self, uri: str) -> tuple[str, str] | None:
        """Find the longest matching template prefix for an expanded URI.

        Returns ``(server_name, template_uri)`` or *None* if no match.
        The match uses the longest static prefix stored in
        ``_template_prefixes`` (the portion of each template URI before
        the first ``{``), with simple ``startswith`` matching.
        """
        best: tuple[str, str] | None = None
        best_len = 0
        for prefix, mapping in self._template_prefixes.items():
            if uri.startswith(prefix) and len(prefix) > best_len:
                best = mapping
                best_len = len(prefix)
        return best

    def read_resource_sync(self, uri: str, timeout: int = 120) -> str:
        """Read a resource by URI synchronously (blocks the calling thread).

        Returns text content for ``TextResourceContents``, or base64 data
        for ``BlobResourceContents``.
        """
        mapping = self._resource_map.get(uri)
        if mapping is None:
            # Fall back to template prefix matching for expanded URIs
            mapping = self._match_template(uri)
        if mapping is None:
            raise ValueError(f"Unknown MCP resource: {uri}")
        server_name, _ = mapping

        self._cb_gate(server_name)

        state = self._static_servers.get(server_name)
        session = state.session if state is not None else None
        if session is None:
            session = self._cb_auto_reconnect(server_name)
        assert self._loop is not None

        future = asyncio.run_coroutine_threadsafe(session.read_resource(uri), self._loop)
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._cb_record_failure(server_name)
            raise TimeoutError(f"MCP resource read timed out after {timeout}s") from None
        except Exception as exc:
            if not isinstance(exc, McpError):
                self._cb_record_failure(server_name)
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, EOFError)):
                # Evict the session only — leave stack/streams behind so the
                # stale-session-and-stack guard in _connect_one cleans them up
                # on the next connect attempt.
                evict = self._static_servers.get(server_name)
                if evict is not None:
                    evict.session = None
            raise

        self._cb_record_success(server_name)

        parts: list[str] = []
        for item in result.contents:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif hasattr(item, "blob"):
                parts.append(item.blob)
            else:
                parts.append(str(item))
        return "\n".join(parts) if parts else "(empty resource)"

    # -- prompt invocation ---------------------------------------------------

    def get_prompt_sync(
        self,
        prefixed_name: str,
        arguments: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> list[dict[str, Any]]:
        """Invoke an MCP prompt synchronously and return expanded messages.

        Returns a list of ``{role: str, content: str}`` dicts.
        """
        mapping = self._prompt_map.get(prefixed_name)
        if mapping is None:
            raise ValueError(f"Unknown MCP prompt: {prefixed_name}")
        server_name, original_name = mapping

        self._cb_gate(server_name)

        state = self._static_servers.get(server_name)
        session = state.session if state is not None else None
        if session is None:
            session = self._cb_auto_reconnect(server_name)
        assert self._loop is not None

        future = asyncio.run_coroutine_threadsafe(
            session.get_prompt(original_name, arguments=arguments), self._loop
        )
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._cb_record_failure(server_name)
            raise TimeoutError(f"MCP prompt retrieval timed out after {timeout}s") from None
        except Exception as exc:
            if not isinstance(exc, McpError):
                self._cb_record_failure(server_name)
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, EOFError)):
                # Evict the session only — leave stack/streams behind so the
                # stale-session-and-stack guard in _connect_one cleans them up
                # on the next connect attempt.
                evict = self._static_servers.get(server_name)
                if evict is not None:
                    evict.session = None
            raise

        self._cb_record_success(server_name)

        messages: list[dict[str, Any]] = []
        for msg in result.messages:
            content = msg.content
            text = content.text if hasattr(content, "text") else str(content)
            messages.append({"role": msg.role, "content": text})
        return messages


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


def _structured_error(
    *,
    code: str,
    server: str,
    detail: str,
    scopes_required: list[str] | None = None,
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
    return json.dumps({"error": err})


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

    Skips ``auth_type='oauth_user'`` rows: they need per-user bearer
    tokens fetched at dispatch time via the OAuth flow, so auto-connecting
    them at startup with empty headers fails handshake and trips the
    circuit breaker. The upcoming per-user pool integration brings them
    online lazily once a user has consented.
    """
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("auth_type") == "oauth_user":
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
    if storage is not None:
        try:
            rows = storage.list_mcp_servers(enabled_only=True)
            if rows:
                db_names = {r["name"] for r in rows}
                # Cache oauth_user names so per-turn callers (web_search
                # backend resolution) can answer auth_type without SQL.
                oauth_user_names = {r["name"] for r in rows if r.get("auth_type") == "oauth_user"}
        except Exception:
            log.warning("Failed to load DB-managed MCP servers", exc_info=True)

    servers = load_mcp_config(config_path, storage=storage)
    if not servers:
        return None

    mgr = MCPClientManager(servers)
    # Mark DB-sourced servers so reconcile_sync won't remove config-file servers
    mgr._db_managed = {name for name in servers if name in db_names}
    mgr._oauth_user_server_names = oauth_user_names
    mgr.start()
    return mgr
