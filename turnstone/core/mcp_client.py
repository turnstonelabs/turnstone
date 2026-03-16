"""MCP (Model Context Protocol) client manager.

Connects to external MCP tool servers and exposes their tools, resources,
and prompts alongside turnstone's built-in capabilities.

Architecture: the MCP SDK is fully async, but turnstone's ChatSession is
synchronous.  We bridge the two by running a dedicated asyncio event loop
in a daemon thread.  ``call_tool_sync`` dispatches coroutines onto that loop
via ``asyncio.run_coroutine_threadsafe``.

Refresh: three mechanisms keep tool/resource/prompt lists up-to-date:
  1. Push notifications — servers declaring ``listChanged`` on the
     respective capability trigger immediate refresh.
  2. Periodic timer — servers *without* push support are polled on a
     staggered interval (configurable, default 4 h, seeded at launch).
  3. Manual — ``/mcp refresh [server]`` triggers ``refresh_sync()``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import random
import threading
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import mcp.types as mcp_types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from turnstone.core.config import load_config

log = logging.getLogger("turnstone.mcp")

_DEFAULT_REFRESH_INTERVAL: float = 14400  # 4 hours


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
            "description": f"[MCP: {server_name}] {description}",
            "parameters": input_schema,
        },
    }


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
        *,
        refresh_interval: float = _DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        self._server_configs = server_configs
        if refresh_interval < 0:
            refresh_interval = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._per_server_stacks: dict[str, AsyncExitStack] = {}

        self._sessions: dict[str, Any] = {}
        self._tools: list[dict[str, Any]] = []
        # prefixed_name -> (server_name, original_tool_name)
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

        # Per-server tool storage for surgical refresh
        self._per_server_tools: dict[str, list[dict[str, Any]]] = {}
        # Tracks which servers support push notifications
        self._supports_list_changed: dict[str, bool] = {}

        # Listener infrastructure (tool-change callbacks for ChatSession)
        self._listeners: list[Callable[[], None]] = []
        self._listeners_lock = threading.Lock()

        # Resources — parallel to tools
        self._per_server_resources: dict[str, list[dict[str, Any]]] = {}
        self._resources: list[dict[str, Any]] = []
        self._resource_map: dict[str, tuple[str, str]] = {}  # uri → (server, uri)
        self._supports_resources: dict[str, bool] = {}  # server has resources capability
        self._supports_resource_list_changed: dict[str, bool] = {}
        self._resource_listeners: list[Callable[[], None]] = []
        self._resource_listeners_lock = threading.Lock()

        # Prompts — parallel to tools
        self._per_server_prompts: dict[str, list[dict[str, Any]]] = {}
        self._prompts: list[dict[str, Any]] = []
        self._prompt_map: dict[str, tuple[str, str]] = {}  # prefixed → (server, original)
        self._supports_prompts: dict[str, bool] = {}  # server has prompts capability
        self._supports_prompt_list_changed: dict[str, bool] = {}
        self._prompt_listeners: list[Callable[[], None]] = []
        self._prompt_listeners_lock = threading.Lock()

        # Template prefix → (server_name, full_template_uri) for URI expansion
        self._template_prefixes: dict[str, tuple[str, str]] = {}

        # Governance storage (optional — set via set_storage())
        self._storage: Any = None
        self._sync_lock = threading.Lock()

        # Periodic refresh for servers without push notifications
        self._refresh_interval = refresh_interval
        self._refresh_task: asyncio.Task[None] | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch background event loop and connect to all configured servers."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp-loop")
        self._thread.start()

        future = asyncio.run_coroutine_threadsafe(self._connect_all(), self._loop)
        self._connected.wait(timeout=30)
        # Surface any exception from _connect_all (unlikely — per-server errors are caught)
        if future.done() and future.exception():
            self._error = str(future.exception())
            log.error("MCP initialization error: %s", self._error)

    async def _connect_all(self) -> None:
        """Connect to every configured server (runs on the background loop)."""
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()

        for name, cfg in self._server_configs.items():
            try:
                await self._connect_one(name, cfg)
            except Exception as exc:
                log.warning("Failed to connect MCP server '%s'", name, exc_info=True)
                self._set_error(name, f"{type(exc).__name__}: {exc}")

        self._connected.set()

        # Start periodic refresh for servers without push notifications
        needs_periodic = any(
            not self._supports_list_changed.get(name, False)
            or (
                self._supports_resources.get(name, False)
                and not self._supports_resource_list_changed.get(name, False)
            )
            or (
                self._supports_prompts.get(name, False)
                and not self._supports_prompt_list_changed.get(name, False)
            )
            for name in self._sessions
        )
        if needs_periodic and self._refresh_interval > 0:
            self._refresh_task = asyncio.get_running_loop().create_task(self._periodic_refresh())

    async def _connect_one(self, name: str, cfg: dict[str, Any]) -> None:
        """Connect to a single MCP server and discover its tools."""
        if "__" in name:
            log.error("MCP server name '%s' contains '__' (reserved delimiter), skipping", name)
            return

        # Per-server exit stack for clean per-server lifecycle management
        stack = AsyncExitStack()
        await stack.__aenter__()

        transport = cfg.get("type", "stdio")
        try:
            if transport in ("http", "streamable-http") or "url" in cfg:
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(url=cfg["url"], headers=cfg.get("headers"))
                )
            else:
                # Default: stdio transport
                command = cfg.get("command", "")
                if not command:
                    log.warning("MCP server '%s' has no command configured", name)
                    await stack.aclose()
                    return
                env = {**os.environ, **cfg.get("env", {})}
                params = StdioServerParameters(
                    command=command,
                    args=cfg.get("args", []),
                    env=env,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
        except Exception:
            await stack.aclose()
            raise

        # Register notification handler — dispatches tool, resource, and
        # prompt list-change notifications to the appropriate refresh method.
        async def _on_notification(
            msg: Any,  # RequestResponder | ServerNotification | Exception
        ) -> None:
            if not isinstance(msg, mcp_types.ServerNotification):
                return
            root = msg.root
            try:
                if isinstance(root, mcp_types.ToolListChangedNotification):
                    log.info("Received tools/list_changed from '%s'", name)
                    await self._refresh_server_tools(name)
                elif isinstance(root, mcp_types.ResourceListChangedNotification):
                    log.info("Received resources/list_changed from '%s'", name)
                    await self._refresh_server_resources(name)
                elif isinstance(root, mcp_types.PromptListChangedNotification):
                    log.info("Received prompts/list_changed from '%s'", name)
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
            await stack.aclose()
            raise

        self._per_server_stacks[name] = stack
        try:
            await session.initialize()
        except Exception:
            self._per_server_stacks.pop(name, None)
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise
        self._sessions[name] = session

        # Check push notification support for each capability
        caps = session.get_server_capabilities()

        tools_cap = getattr(caps, "tools", None) if caps else None
        self._supports_list_changed[name] = bool(getattr(tools_cap, "listChanged", False))

        resources_cap = getattr(caps, "resources", None) if caps else None
        self._supports_resources[name] = resources_cap is not None
        self._supports_resource_list_changed[name] = bool(
            getattr(resources_cap, "listChanged", False)
        )

        prompts_cap = getattr(caps, "prompts", None) if caps else None
        self._supports_prompts[name] = prompts_cap is not None
        self._supports_prompt_list_changed[name] = bool(getattr(prompts_cap, "listChanged", False))

        # Discover tools
        result = await session.list_tools()
        server_tools: list[dict[str, Any]] = []
        for tool in result.tools:
            server_tools.append(_mcp_to_openai(name, tool))

        self._per_server_tools[name] = server_tools
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
            self._per_server_resources[name] = server_resources
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
            self._per_server_prompts[name] = server_prompts
            self._rebuild_prompts()

        push_parts: list[str] = []
        if self._supports_list_changed[name]:
            push_parts.append("tools")
        if self._supports_resource_list_changed[name]:
            push_parts.append("resources")
        if self._supports_prompt_list_changed[name]:
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

    # -- tool refresh --------------------------------------------------------

    def _rebuild_tools(self) -> None:
        """Rebuild merged ``_tools`` and ``_tool_map`` from per-server state.

        Uses copy-on-write: builds new objects, then assigns atomically.
        Concurrent readers see either the old or new snapshot — both valid.
        """
        new_tools: list[dict[str, Any]] = []
        new_map: dict[str, tuple[str, str]] = {}
        for srv_name, srv_tools in self._per_server_tools.items():
            for tool in srv_tools:
                prefixed: str = tool["function"]["name"]
                new_tools.append(tool)
                # Extract original name from the mcp__server__original pattern
                original = prefixed.split("__", 2)[2] if prefixed.count("__") >= 2 else prefixed
                new_map[prefixed] = (srv_name, original)
        self._tools = new_tools
        self._tool_map = new_map
        self._notify_listeners()

    async def _refresh_server_tools(self, name: str) -> tuple[list[str], list[str]]:
        """Re-fetch tools for one server.  Returns ``(added, removed)`` names."""
        session = self._sessions.get(name)
        if session is None:
            raise RuntimeError(f"MCP server '{name}' is not connected")

        old_names = {t["function"]["name"] for t in self._per_server_tools.get(name, [])}

        result = await session.list_tools()
        server_tools = [_mcp_to_openai(name, tool) for tool in result.tools]
        new_names = {t["function"]["name"] for t in server_tools}

        self._per_server_tools[name] = server_tools
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

    async def _refresh_server(self, name: str) -> tuple[list[str], list[str]]:
        """Re-fetch tools, resources, and prompts for one server.

        Returns ``(added_tools, removed_tools)`` names (tool diff only,
        for backward compatibility with ``/mcp refresh`` output).
        """
        added, removed = await self._refresh_server_tools(name)
        await self._refresh_server_resources(name)
        await self._refresh_server_prompts(name)
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
                if name not in self._sessions:
                    # Attempt reconnect
                    cfg = self._server_configs.get(name)
                    if cfg:
                        log.info("Reconnecting MCP server '%s'", name)
                        await self._connect_one(name, cfg)
                        new_names = [
                            t["function"]["name"] for t in self._per_server_tools.get(name, [])
                        ]
                        results[name] = (new_names, [])
                    continue
                added, removed = await self._refresh_server(name)
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
        return future.result(timeout=timeout)

    async def _periodic_refresh(self) -> None:
        """Periodically refresh servers that lack push notifications."""
        # Stagger start using a launch-time seed so cluster nodes don't
        # all hit MCP servers simultaneously.
        seed = random.Random(time.monotonic_ns() ^ os.getpid()).random()
        initial_delay = seed * self._refresh_interval
        await asyncio.sleep(initial_delay)
        while True:
            for name in list(self._server_configs):
                if name not in self._sessions:
                    continue  # not connected — skip (reconnect on manual refresh)
                try:
                    if not self._supports_list_changed.get(name, False):
                        await self._refresh_server_tools(name)
                    if not self._supports_resource_list_changed.get(name, False):
                        await self._refresh_server_resources(name)
                    if not self._supports_prompt_list_changed.get(name, False):
                        await self._refresh_server_prompts(name)
                    self._last_error.pop(name, None)
                except Exception as exc:
                    log.warning("Periodic refresh failed for '%s'", name, exc_info=True)
                    self._set_error(name, f"Periodic refresh failed: {exc}")
            await asyncio.sleep(self._refresh_interval)

    # -- resource refresh ----------------------------------------------------

    def _rebuild_resources(self) -> None:
        """Rebuild merged ``_resources`` and ``_resource_map`` from per-server state.

        Uses copy-on-write: builds new objects, then assigns atomically.
        """
        new_resources: list[dict[str, Any]] = []
        new_map: dict[str, tuple[str, str]] = {}
        for srv_name, srv_resources in self._per_server_resources.items():
            for res in srv_resources:
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
        for srv_name, srv_resources in self._per_server_resources.items():
            for res in srv_resources:
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
        if not self._supports_resources.get(name, False):
            return
        session = self._sessions.get(name)
        if session is None:
            return

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

        self._per_server_resources[name] = server_resources
        self._rebuild_resources()

    # -- prompt refresh ------------------------------------------------------

    def _rebuild_prompts(self) -> None:
        """Rebuild merged ``_prompts`` and ``_prompt_map`` from per-server state.

        Uses copy-on-write: builds new objects, then assigns atomically.
        """
        new_prompts: list[dict[str, Any]] = []
        new_map: dict[str, tuple[str, str]] = {}
        for srv_name, srv_prompts in self._per_server_prompts.items():
            for prompt in srv_prompts:
                prefixed: str = prompt["name"]
                new_prompts.append(prompt)
                new_map[prefixed] = (srv_name, prompt["original_name"])
        self._prompts = new_prompts
        self._prompt_map = new_map
        self._notify_prompt_listeners()

    async def _refresh_server_prompts(self, name: str) -> None:
        """Re-fetch prompts for one server."""
        if not self._supports_prompts.get(name, False):
            return
        session = self._sessions.get(name)
        if session is None:
            return

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

        self._per_server_prompts[name] = server_prompts
        self._rebuild_prompts()

        # Sync discovered prompts into governance storage
        try:
            self.sync_prompts_to_storage()
        except Exception:
            log.warning("Prompt sync after refresh failed for '%s'", name, exc_info=True)

    # -- listener infrastructure ---------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the tool list changes."""
        with self._listeners_lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        """Unregister a tool-change callback."""
        with self._listeners_lock, contextlib.suppress(ValueError):
            self._listeners.remove(callback)

    def _notify_listeners(self) -> None:
        """Invoke all registered tool-change listeners."""
        with self._listeners_lock:
            listeners = list(self._listeners)
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
                )
            else:
                # Create new MCP-sourced template
                template_id = str(uuid.uuid4())
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
        # Cancel periodic refresh
        if self._refresh_task and self._loop:
            self._loop.call_soon_threadsafe(self._refresh_task.cancel)

        # Close all per-server stacks (transports + sessions)
        if self._loop and self._per_server_stacks:

            async def _close_all_stacks() -> None:
                for stack in self._per_server_stacks.values():
                    with contextlib.suppress(Exception):
                        await stack.aclose()

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
        self._sessions.clear()
        self._per_server_stacks.clear()
        self._db_managed.clear()
        self._tools = []
        self._tool_map = {}
        self._per_server_tools.clear()
        self._supports_list_changed.clear()
        self._resources = []
        self._resource_map = {}
        self._template_prefixes = {}
        self._per_server_resources.clear()
        self._supports_resources.clear()
        self._supports_resource_list_changed.clear()
        self._prompts = []
        self._prompt_map = {}
        self._per_server_prompts.clear()
        self._supports_prompts.clear()
        self._supports_prompt_list_changed.clear()
        # Clear listener lists to release callback references
        self._listeners.clear()
        self._resource_listeners.clear()
        self._prompt_listeners.clear()

        log.info("MCP client shut down")

    # -- hot-reload (add/remove servers) ------------------------------------

    def add_server_sync(self, name: str, cfg: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        """Connect a new MCP server at runtime (blocks the calling thread).

        Returns status dict with keys: connected, tools, resources, prompts, error.
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

        return {
            "connected": name in self._sessions,
            "tools": len(self._per_server_tools.get(name, [])),
            "resources": len(self._per_server_resources.get(name, [])),
            "prompts": len(self._per_server_prompts.get(name, [])),
            "error": "",
        }

    def remove_server_sync(self, name: str, timeout: int = 15) -> bool:
        """Disconnect and remove an MCP server at runtime (blocks the calling thread).

        All state mutations run on the MCP event loop thread to avoid races
        with notification handlers and refresh tasks.

        Returns True if the server was connected and successfully removed.
        """
        was_connected = name in self._sessions

        # Remove from config to prevent reconnection
        self._server_configs.pop(name, None)

        if self._loop is not None:

            async def _remove() -> None:
                # Close session + transport via per-server stack
                self._sessions.pop(name, None)
                stack = self._per_server_stacks.pop(name, None)
                if stack is not None:
                    with contextlib.suppress(Exception):
                        await stack.aclose()
                # Clean up per-server state (on the event loop thread)
                self._per_server_tools.pop(name, None)
                self._per_server_resources.pop(name, None)
                self._per_server_prompts.pop(name, None)
                self._supports_list_changed.pop(name, None)
                self._supports_resources.pop(name, None)
                self._supports_resource_list_changed.pop(name, None)
                self._supports_prompts.pop(name, None)
                self._supports_prompt_list_changed.pop(name, None)
                self._last_error.pop(name, None)
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
            self._sessions.pop(name, None)
            self._per_server_tools.pop(name, None)
            self._per_server_resources.pop(name, None)
            self._per_server_prompts.pop(name, None)
            self._supports_list_changed.pop(name, None)
            self._supports_resources.pop(name, None)
            self._supports_resource_list_changed.pop(name, None)
            self._supports_prompts.pop(name, None)
            self._supports_prompt_list_changed.pop(name, None)
            self._last_error.pop(name, None)
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
        connected = name in self._sessions
        cfg = self._server_configs.get(name, {})
        transport = cfg.get("type", "stdio")
        return {
            "connected": connected,
            "tools": len(self._per_server_tools.get(name, [])) if connected else 0,
            "resources": len(self._per_server_resources.get(name, [])) if connected else 0,
            "prompts": len(self._per_server_prompts.get(name, [])) if connected else 0,
            "error": self._last_error.get(name, ""),
            "transport": transport,
            "command": cfg.get("command", "") if transport == "stdio" else "",
            "url": cfg.get("url", "") if transport != "stdio" else "",
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

    def get_tools(self) -> list[dict[str, Any]]:
        """Return MCP tools in OpenAI function-calling format."""
        return [dict(t) for t in self._tools]

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

    def is_mcp_tool(self, func_name: str) -> bool:
        """Check whether *func_name* belongs to an MCP server."""
        return func_name in self._tool_map

    def is_mcp_prompt(self, name: str) -> bool:
        """Check whether *name* is a known MCP prompt."""
        return name in self._prompt_map

    @property
    def server_count(self) -> int:
        return len(self._sessions)

    @property
    def error_count(self) -> int:
        """Number of servers currently in error state."""
        return len(self._last_error)

    @property
    def server_names(self) -> list[str]:
        """Return configured server names."""
        return list(self._server_configs.keys())

    # -- tool invocation -----------------------------------------------------

    def call_tool_sync(
        self,
        func_name: str,
        arguments: dict[str, Any],
        timeout: int = 120,
    ) -> str:
        """Execute an MCP tool call synchronously (blocks the calling thread).

        Dispatches an async ``tools/call`` to the background event loop and
        waits for the result.
        """
        mapping = self._tool_map.get(func_name)
        if mapping is None:
            raise ValueError(f"Unknown MCP tool: {func_name}")
        server_name, original_name = mapping
        session = self._sessions.get(server_name)
        if session is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected")
        assert self._loop is not None

        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(original_name, arguments), self._loop
        )
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"MCP tool call timed out after {timeout}s") from None

        # Extract text from the content array
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
        session = self._sessions.get(server_name)
        if session is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected")
        assert self._loop is not None

        future = asyncio.run_coroutine_threadsafe(session.read_resource(uri), self._loop)
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"MCP resource read timed out after {timeout}s") from None

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
        session = self._sessions.get(server_name)
        if session is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected")
        assert self._loop is not None

        future = asyncio.run_coroutine_threadsafe(
            session.get_prompt(original_name, arguments=arguments), self._loop
        )
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"MCP prompt retrieval timed out after {timeout}s") from None

        messages: list[dict[str, Any]] = []
        for msg in result.messages:
            content = msg.content
            text = content.text if hasattr(content, "text") else str(content)
            messages.append({"role": msg.role, "content": text})
        return messages


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _db_servers_to_config(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Convert mcp_servers DB rows to the config dict format."""
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
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
    refresh_interval: float = _DEFAULT_REFRESH_INTERVAL,
    storage: Any = None,
) -> MCPClientManager | None:
    """Create and start an MCP client manager.

    Returns *None* if no servers are configured.
    """
    # Check DB first to know which servers are DB-managed
    db_names: set[str] = set()
    if storage is not None:
        try:
            rows = storage.list_mcp_servers(enabled_only=True)
            if rows:
                db_names = {r["name"] for r in rows}
        except Exception:
            pass

    servers = load_mcp_config(config_path, storage=storage)
    if not servers:
        return None

    mgr = MCPClientManager(servers, refresh_interval=refresh_interval)
    # Mark DB-sourced servers so reconcile_sync won't remove config-file servers
    mgr._db_managed = {name for name in servers if name in db_names}
    mgr.start()
    return mgr
