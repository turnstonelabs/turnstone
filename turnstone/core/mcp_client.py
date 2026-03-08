"""MCP (Model Context Protocol) client manager.

Connects to external MCP tool servers and exposes their tools alongside
turnstone's built-in tools.

Architecture: the MCP SDK is fully async, but turnstone's ChatSession is
synchronous.  We bridge the two by running a dedicated asyncio event loop
in a daemon thread.  ``call_tool_sync`` dispatches coroutines onto that loop
via ``asyncio.run_coroutine_threadsafe``.

Tool refresh: three mechanisms keep tool lists up-to-date without restart:
  1. Push notifications — servers declaring ``tools.listChanged`` trigger
     immediate refresh via ``ToolListChangedNotification``.
  2. Periodic timer — servers *without* push support are polled on a
     staggered interval (configurable, default 4 h, seeded at launch).
  3. Manual — ``/mcp refresh [server]`` triggers ``refresh_sync()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import threading
import time
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

        self._sessions: dict[str, Any] = {}
        self._tools: list[dict[str, Any]] = []
        # prefixed_name -> (server_name, original_tool_name)
        self._tool_map: dict[str, tuple[str, str]] = {}
        self._connected = threading.Event()
        self._error: str | None = None

        # Per-server tool storage for surgical refresh
        self._per_server_tools: dict[str, list[dict[str, Any]]] = {}
        # Tracks which servers support push notifications
        self._supports_list_changed: dict[str, bool] = {}

        # Listener infrastructure (tool-change callbacks for ChatSession)
        self._listeners: list[Callable[[], None]] = []
        self._listeners_lock = threading.Lock()

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
            except Exception:
                log.warning("Failed to connect MCP server '%s'", name, exc_info=True)

        self._connected.set()

        # Start periodic refresh for servers without push notifications
        needs_periodic = any(
            not self._supports_list_changed.get(name, False) for name in self._sessions
        )
        if needs_periodic and self._refresh_interval > 0:
            self._refresh_task = asyncio.get_running_loop().create_task(self._periodic_refresh())

    async def _connect_one(self, name: str, cfg: dict[str, Any]) -> None:
        """Connect to a single MCP server and discover its tools."""
        assert self._exit_stack is not None

        if "__" in name:
            log.error("MCP server name '%s' contains '__' (reserved delimiter), skipping", name)
            return

        transport = cfg.get("type", "stdio")
        if transport in ("http", "streamable-http") or "url" in cfg:
            read, write, _ = await self._exit_stack.enter_async_context(
                streamablehttp_client(url=cfg["url"], headers=cfg.get("headers"))
            )
        else:
            # Default: stdio transport
            command = cfg.get("command", "")
            if not command:
                log.warning("MCP server '%s' has no command configured", name)
                return
            env = {**os.environ, **cfg.get("env", {})}
            params = StdioServerParameters(
                command=command,
                args=cfg.get("args", []),
                env=env,
            )
            read, write = await self._exit_stack.enter_async_context(stdio_client(params))

        # Register notification handler — lightweight; only acts on
        # ToolListChangedNotification, which is a no-op if the server
        # never sends it.
        async def _on_notification(
            msg: Any,  # RequestResponder | ServerNotification | Exception
        ) -> None:
            if isinstance(msg, mcp_types.ServerNotification) and isinstance(
                msg.root, mcp_types.ToolListChangedNotification
            ):
                log.info("Received tools/list_changed from '%s'", name)
                try:
                    await self._refresh_server(name)
                except Exception:
                    log.warning("Refresh after notification failed for '%s'", name, exc_info=True)

        session = await self._exit_stack.enter_async_context(
            ClientSession(read, write, message_handler=_on_notification)  # type: ignore[arg-type]
        )
        await session.initialize()
        self._sessions[name] = session

        # Check push notification support
        caps = session.get_server_capabilities()
        tools_cap = getattr(caps, "tools", None) if caps else None
        self._supports_list_changed[name] = bool(getattr(tools_cap, "listChanged", False))

        # Discover tools
        result = await session.list_tools()
        server_tools: list[dict[str, Any]] = []
        for tool in result.tools:
            server_tools.append(_mcp_to_openai(name, tool))

        self._per_server_tools[name] = server_tools
        self._rebuild_tools()

        push_status = " (push)" if self._supports_list_changed[name] else ""
        log.info(
            "Connected MCP server '%s' — %d tool(s)%s",
            name,
            len(result.tools),
            push_status,
        )

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

    async def _refresh_server(self, name: str) -> tuple[list[str], list[str]]:
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

    async def _refresh_all(
        self, server_name: str | None = None
    ) -> dict[str, tuple[list[str], list[str]]]:
        """Refresh tools for one or all servers.

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
            except Exception:
                log.warning("Refresh failed for MCP server '%s'", name, exc_info=True)
                results[name] = ([], [])
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
                if self._supports_list_changed.get(name, False):
                    continue  # has push — skip
                if name not in self._sessions:
                    continue  # not connected — skip (reconnect on manual refresh)
                try:
                    await self._refresh_server(name)
                except Exception:
                    log.warning("Periodic refresh failed for '%s'", name, exc_info=True)
            await asyncio.sleep(self._refresh_interval)

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
        """Invoke all registered listeners (runs on MCP background thread)."""
        with self._listeners_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb()
            except Exception:
                log.warning("Tool-change listener raised", exc_info=True)

    # -- lifecycle (shutdown) ------------------------------------------------

    def shutdown(self) -> None:
        """Close all MCP sessions and stop the background loop."""
        # Cancel periodic refresh
        if self._refresh_task and self._loop:
            self._loop.call_soon_threadsafe(self._refresh_task.cancel)

        if self._loop and self._exit_stack:
            future = asyncio.run_coroutine_threadsafe(self._exit_stack.aclose(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                log.debug("Error closing MCP sessions", exc_info=True)

        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

        log.info("MCP client shut down")

    # -- query methods -------------------------------------------------------

    def get_tools(self) -> list[dict[str, Any]]:
        """Return MCP tools in OpenAI function-calling format."""
        return list(self._tools)

    def is_mcp_tool(self, func_name: str) -> bool:
        """Check whether *func_name* belongs to an MCP server."""
        return func_name in self._tool_map

    @property
    def server_count(self) -> int:
        return len(self._sessions)

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
        result = future.result(timeout=timeout)

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


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_mcp_config(config_path: str | None = None) -> dict[str, dict[str, Any]]:
    """Load MCP server configurations.

    Sources (first match wins):

    1. Explicit *config_path* (standard MCP JSON format).
    2. ``[mcp.servers.*]`` sections in ``config.toml``.

    Returns an empty dict if nothing is configured.
    """
    # 1. Explicit JSON file
    if config_path:
        path = Path(config_path).expanduser()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                servers: dict[str, Any] = data.get("mcpServers", {})
                if isinstance(servers, dict) and servers:
                    log.info("Loaded MCP config from %s (%d server(s))", path, len(servers))
                    return servers
            except Exception:
                log.warning("Failed to parse MCP config file: %s", path, exc_info=True)
        else:
            log.warning("MCP config file not found: %s", path)

    # 2. TOML config
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
) -> MCPClientManager | None:
    """Create and start an MCP client manager.

    Returns *None* if no servers are configured.
    """
    servers = load_mcp_config(config_path)
    if not servers:
        return None

    mgr = MCPClientManager(servers, refresh_interval=refresh_interval)
    mgr.start()
    return mgr
