"""MCP (Model Context Protocol) client manager.

Connects to external MCP tool servers and exposes their tools alongside
turnstone's built-in tools.

Architecture: the MCP SDK is fully async, but turnstone's ChatSession is
synchronous.  We bridge the two by running a dedicated asyncio event loop
in a daemon thread.  ``call_tool_sync`` dispatches coroutines onto that loop
via ``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from turnstone.core.config import load_config

log = logging.getLogger("turnstone.mcp")


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

    def __init__(self, server_configs: dict[str, dict[str, Any]]) -> None:
        self._server_configs = server_configs
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._exit_stack: AsyncExitStack | None = None

        self._sessions: dict[str, Any] = {}
        self._tools: list[dict[str, Any]] = []
        # prefixed_name -> (server_name, original_tool_name)
        self._tool_map: dict[str, tuple[str, str]] = {}
        self._connected = threading.Event()
        self._error: str | None = None

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

        session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[name] = session

        # Discover tools
        result = await session.list_tools()
        for tool in result.tools:
            openai_def = _mcp_to_openai(name, tool)
            prefixed = openai_def["function"]["name"]
            self._tools.append(openai_def)
            self._tool_map[prefixed] = (name, tool.name)

        log.info(
            "Connected MCP server '%s' — %d tool(s)",
            name,
            len(result.tools),
        )

    def shutdown(self) -> None:
        """Close all MCP sessions and stop the background loop."""
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


def create_mcp_client(config_path: str | None = None) -> MCPClientManager | None:
    """Create and start an MCP client manager.

    Returns *None* if no servers are configured.
    """
    servers = load_mcp_config(config_path)
    if not servers:
        return None

    mgr = MCPClientManager(servers)
    mgr.start()
    return mgr
