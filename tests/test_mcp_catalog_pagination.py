"""MCP catalogs are read past their first page (#1225).

A real low-level MCP server, served over streamable HTTP in this process, pages
each of its four catalogs (tools, prompts, resources, resource templates) two
items at a time. The tests connect to it through ``MCPClientManager`` on the
static and the per-user pool paths, then change the catalogs and refresh, so
every list call issued on connect and on refresh crosses the real SDK client
and server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import textwrap
import threading
from collections import Counter
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import mcp.types as mcp_types
import pytest
import uvicorn
from mcp import McpError
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Mount

from tests.conftest import _free_port, _run_on_loop, _wait_tcp_ready, serve_until_exit
from turnstone.core.mcp_client import (
    InvalidCatalogError,
    MCPClientManager,
    PoolEntryState,
    _AuthCapture,
    _list_catalog,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from pathlib import Path

logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

PAGE_SIZE = 2
KINDS = ("tools", "prompts", "resources", "templates")


class PagedCatalog:
    """The server's four catalogs and how it pages them.

    ``mode`` selects how the server answers a cursor:

    * ``"offset"``: the cursor is the next item's index, until the list runs out;
    * ``"repeat"``: always ``"again"``, which serves the second page forever;
    * ``"empty-pages"``: an empty page and a fresh cursor, forever;
    * ``"ignores-cursor"``: the first page again, with a fresh cursor, forever;
    * ``"gaps"``: like ``"offset"``, with two empty pages between the first and second pages;
    * ``"rejects-cursor"``: a JSON-RPC invalid-params error.
    """

    def __init__(self) -> None:
        self.items: dict[str, list[str]] = {kind: [] for kind in KINDS}
        self.mode = "offset"
        self.requests: Counter[str] = Counter()
        self.before_page: Callable[[str, str | None], Awaitable[None]] | None = None

    def fill(self, names: range) -> None:
        for kind in KINDS:
            self.items[kind] = [f"{kind}_{i}" for i in names]

    def page(self, kind: str, cursor: str | None) -> tuple[list[str], str | None]:
        self.requests[kind] += 1
        names = self.items[kind]
        fresh = f"fresh-{self.requests[kind]}"
        if self.mode == "rejects-cursor" and cursor is not None:
            raise McpError(mcp_types.ErrorData(code=mcp_types.INVALID_PARAMS, message="bad cursor"))
        if self.mode == "empty-pages":
            return (names[:PAGE_SIZE] if cursor is None else []), fresh
        if self.mode in ("ignores-cursor", "rejects-cursor"):
            return names[:PAGE_SIZE], fresh
        if self.mode == "gaps" and cursor in (None, "gap-1", "gap-2"):
            if cursor is None:
                return names[:PAGE_SIZE], "gap-1"
            return [], ("gap-2" if cursor == "gap-1" else str(PAGE_SIZE))
        if self.mode == "repeat":
            start = 0 if cursor is None else PAGE_SIZE
            return names[start : start + PAGE_SIZE], "again"
        start = int(cursor or 0)
        end = start + PAGE_SIZE
        return names[start:end], (str(end) if end < len(names) else None)


def _build_server(catalog: PagedCatalog) -> Server[Any, Any]:
    server: Server[Any, Any] = Server("paged")

    def _serve(kind: str, request_type: type, build: Any) -> None:
        async def _handle(request: Any) -> mcp_types.ServerResult:
            cursor = request.params.cursor if request.params is not None else None
            if catalog.before_page is not None:
                await catalog.before_page(kind, cursor)
            names, next_cursor = catalog.page(kind, cursor)
            return mcp_types.ServerResult(build(names, next_cursor))

        server.request_handlers[request_type] = _handle

    _serve(
        "tools",
        mcp_types.ListToolsRequest,
        lambda names, cursor: mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(
                    name=n,
                    description=f"request {catalog.requests['tools']}",
                    inputSchema={"type": "object", "properties": {}},
                )
                for n in names
            ],
            nextCursor=cursor,
        ),
    )
    _serve(
        "prompts",
        mcp_types.ListPromptsRequest,
        lambda names, cursor: mcp_types.ListPromptsResult(
            prompts=[mcp_types.Prompt(name=n) for n in names], nextCursor=cursor
        ),
    )
    _serve(
        "resources",
        mcp_types.ListResourcesRequest,
        lambda names, cursor: mcp_types.ListResourcesResult(
            resources=[mcp_types.Resource(uri=AnyUrl(f"res://{n}"), name=n) for n in names],
            nextCursor=cursor,
        ),
    )
    _serve(
        "templates",
        mcp_types.ListResourceTemplatesRequest,
        lambda names, cursor: mcp_types.ListResourceTemplatesResult(
            resourceTemplates=[
                mcp_types.ResourceTemplate(uriTemplate=f"res://{n}/{{id}}", name=n) for n in names
            ],
            nextCursor=cursor,
        ),
    )
    return server


@pytest.fixture
def paged_server() -> Iterator[tuple[str, PagedCatalog]]:
    catalog = PagedCatalog()
    sessions = StreamableHTTPSessionManager(app=_build_server(catalog))

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with sessions.run():
            yield

    app = Starlette(routes=[Mount("/", app=sessions.handle_request)], lifespan=_lifespan)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=0,
        )
    )
    thread = threading.Thread(
        target=serve_until_exit, args=(server,), daemon=True, name="paged-mcp-upstream"
    )
    thread.start()
    try:
        assert _wait_tcp_ready(port, 5.0), "paged MCP server did not come up"
        yield f"http://127.0.0.1:{port}/mcp", catalog
    finally:
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=5)


@contextlib.contextmanager
def _started_manager(servers: dict[str, Any]) -> Iterator[MCPClientManager]:
    """A started manager whose health loop stays out of the test's way."""
    with patch(
        "turnstone.core.mcp_client.load_config",
        return_value={"static_health_check_seconds": 30},
    ):
        mgr = MCPClientManager(servers)
    mgr.start()
    try:
        yield mgr
    finally:
        mgr.shutdown()


def _published(
    server: str, tools: list[Any], prompts: list[Any], resources: list[Any]
) -> dict[str, list[str]]:
    """Original names of a published catalog, by kind."""
    return {
        "tools": [t["function"]["name"].removeprefix(f"mcp__{server}__") for t in tools],
        "prompts": [p["original_name"] for p in prompts],
        "resources": [r["name"] for r in resources if not r.get("template")],
        "templates": [r["name"] for r in resources if r.get("template")],
    }


def _static_catalog(mgr: MCPClientManager, server: str) -> dict[str, list[str]]:
    state = mgr._static_servers[server]
    return _published(server, state.tools, state.prompts, state.resources)


def _pool_catalog(entry: PoolEntryState, server: str) -> dict[str, list[str]]:
    return _published(server, entry.tools or [], entry.prompts or [], entry.resources or [])


def _connect_pool(mgr: MCPClientManager, key: tuple[str, str], url: str) -> PoolEntryState:
    async def _connect() -> PoolEntryState:
        return await mgr._connect_one_pool(
            key,
            {"type": "streamable-http", "url": url, "headers": {}},
            "access-token",
            auth_capture=_AuthCapture(),
            auth_fired_event=asyncio.Event(),
        )

    assert mgr._loop is not None
    entry: PoolEntryState = _run_on_loop(mgr._loop, _connect())
    return entry


def _cap_every_catalog(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    for name in (
        "_MAX_TOOLS_PER_SERVER",
        "_MAX_PROMPTS_PER_SERVER",
        "_MAX_RESOURCES_PER_SERVER",
        "_MAX_RESOURCE_TEMPLATES_PER_SERVER",
    ):
        monkeypatch.setattr(f"turnstone.core.mcp_client.{name}", cap)


def _refresh_pool(mgr: MCPClientManager, key: tuple[str, str], entry: PoolEntryState) -> None:
    """Run the three pool refreshes a list_changed notification would, under its lock."""

    async def _refresh() -> None:
        async with entry.open_lock:
            await mgr._refresh_pool_server_tools(key)
            await mgr._refresh_pool_server_resources(key)
            await mgr._refresh_pool_server_prompts(key)

    assert mgr._loop is not None
    _run_on_loop(mgr._loop, _refresh())


def test_static_connect_and_refresh_publish_every_page(
    paged_server: tuple[str, PagedCatalog],
) -> None:
    url, catalog = paged_server
    catalog.fill(range(5))

    with _started_manager({"paged": {"type": "http", "url": url}}) as mgr:
        assert _static_catalog(mgr, "paged") == catalog.items
        assert catalog.requests == {kind: 3 for kind in KINDS}

        catalog.fill(range(1, 8))
        added, removed = mgr.refresh_sync("paged", timeout=15)["paged"] or ([], [])
        assert added == ["mcp__paged__tools_5", "mcp__paged__tools_6", "mcp__paged__tools_7"]
        assert removed == ["mcp__paged__tools_0"]
        assert _static_catalog(mgr, "paged") == catalog.items


@pytest.mark.parametrize("phase", ["startup", "reconnect", "add"])
def test_static_resource_pages_overlap(paged_server: tuple[str, PagedCatalog], phase: str) -> None:
    """Both walks must reach each page before the server answers either one."""
    url, catalog = paged_server
    catalog.fill(range(5))
    barriers: dict[str | None, asyncio.Barrier] = {}
    paired_pages: set[str | None] = set()

    async def _rendezvous(kind: str, cursor: str | None) -> None:
        if kind not in ("resources", "templates"):
            return
        barrier = barriers.setdefault(cursor, asyncio.Barrier(2))
        async with asyncio.timeout(5):
            await barrier.wait()
        paired_pages.add(cursor)

    cfg = {"type": "http", "url": url}
    if phase == "startup":
        catalog.before_page = _rendezvous
    with _started_manager({} if phase == "add" else {"paged": cfg}) as mgr:
        if phase != "startup":
            catalog.requests.clear()
            catalog.before_page = _rendezvous
            if phase == "add":
                result = mgr.add_server_sync("paged", cfg, timeout=10)
            else:
                result = mgr.reconnect_sync("paged", timeout=10)
            assert result["connected"], result

        assert _static_catalog(mgr, "paged") == catalog.items
        assert catalog.requests == {kind: 3 for kind in KINDS}
        assert paired_pages == {None, "2", "4"}


def test_pool_connect_and_refresh_publish_every_page(
    paged_server: tuple[str, PagedCatalog],
) -> None:
    url, catalog = paged_server
    catalog.fill(range(5))
    key = ("user-1", "paged")

    with _started_manager({}) as mgr:
        entry = _connect_pool(mgr, key, url)
        assert _pool_catalog(entry, "paged") == catalog.items
        assert mgr.is_mcp_tool("mcp__paged__tools_4", user_id="user-1")

        catalog.fill(range(1, 8))
        _refresh_pool(mgr, key, entry)
        assert _pool_catalog(entry, "paged") == catalog.items
        assert mgr.is_mcp_tool("mcp__paged__tools_7", user_id="user-1")
        assert not mgr.is_mcp_tool("mcp__paged__tools_0", user_id="user-1")


def test_caps_truncate_across_pages_and_stop_reading(
    paged_server: tuple[str, PagedCatalog],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url, catalog = paged_server
    catalog.fill(range(9))
    _cap_every_catalog(monkeypatch, 3)

    with (
        caplog.at_level(logging.WARNING, logger="turnstone.mcp"),
        _started_manager({"paged": {"type": "http", "url": url}}) as mgr,
    ):
        published = _static_catalog(mgr, "paged")

    assert published == {kind: names[:3] for kind, names in catalog.items.items()}
    # The second page takes each list past the cap, so the walk stops there
    # instead of reading all five pages.
    assert catalog.requests == {kind: 2 for kind in KINDS}
    truncations = [r for r in caplog.records if "or more" in r.getMessage()]
    assert len(truncations) == 4


def test_repeated_cursor_stops_the_walk(
    paged_server: tuple[str, PagedCatalog], caplog: pytest.LogCaptureFixture
) -> None:
    url, catalog = paged_server
    catalog.fill(range(6))
    catalog.mode = "repeat"

    with (
        caplog.at_level(logging.WARNING, logger="turnstone.mcp"),
        _started_manager({"paged": {"type": "http", "url": url}}) as mgr,
    ):
        published = _static_catalog(mgr, "paged")

    # Pages one and two are kept; the cursor that would ask for page two
    # again is never sent.
    assert published == {kind: names[:4] for kind, names in catalog.items.items()}
    assert catalog.requests == {kind: 2 for kind in KINDS}
    repeats = [r for r in caplog.records if "repeated a cursor" in r.getMessage()]
    assert len(repeats) == 4


@pytest.mark.parametrize("mode", ["empty-pages", "ignores-cursor"])
def test_a_run_of_pages_with_nothing_new_stops_the_walk(
    paged_server: tuple[str, PagedCatalog],
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url, catalog = paged_server
    catalog.fill(range(6))
    catalog.mode = mode
    monkeypatch.setattr("turnstone.core.mcp_client._MAX_PAGES_WITHOUT_PROGRESS", 3)

    with (
        caplog.at_level(logging.WARNING, logger="turnstone.mcp"),
        _started_manager({"paged": {"type": "http", "url": url}}) as mgr,
    ):
        published = _static_catalog(mgr, "paged")
        descriptions = {t["function"]["description"] for t in mgr._static_servers["paged"].tools}

    # Page one is kept once; after three follow-ups in a row add nothing new, the
    # walk stops however many fresh cursors the server would hand out.
    assert published == {kind: names[:2] for kind, names in catalog.items.items()}
    # A tool listed twice keeps its last definition, as the SDK's schema cache does.
    assert descriptions == {"request 4" if mode == "ignores-cursor" else "request 1"}
    assert catalog.requests == {kind: 4 for kind in KINDS}
    stops = [r for r in caplog.records if "with nothing new" in r.getMessage()]
    assert len(stops) == 4


def test_empty_pages_mid_catalog_do_not_stop_the_walk(
    paged_server: tuple[str, PagedCatalog],
) -> None:
    url, catalog = paged_server
    catalog.fill(range(5))
    catalog.mode = "gaps"

    with _started_manager({"paged": {"type": "http", "url": url}}) as mgr:
        assert _static_catalog(mgr, "paged") == catalog.items
    # Three offset pages plus the two empty ones.
    assert catalog.requests == {kind: 5 for kind in KINDS}


def test_later_page_error_keeps_what_connect_read(
    paged_server: tuple[str, PagedCatalog], caplog: pytest.LogCaptureFixture
) -> None:
    url, catalog = paged_server
    catalog.fill(range(6))
    catalog.mode = "rejects-cursor"

    with (
        caplog.at_level(logging.WARNING, logger="turnstone.mcp"),
        _started_manager({"paged": {"type": "http", "url": url}}) as mgr,
    ):
        assert mgr._static_servers["paged"].session is not None
        published = _static_catalog(mgr, "paged")

    assert published == {kind: names[:2] for kind, names in catalog.items.items()}
    failures = [r for r in caplog.records if "failed a later page" in r.getMessage()]
    assert len(failures) == 4


def test_later_page_error_keeps_the_pages_a_refresh_read(
    paged_server: tuple[str, PagedCatalog],
) -> None:
    url, catalog = paged_server
    catalog.fill(range(5))

    with _started_manager({"paged": {"type": "http", "url": url}}) as mgr:
        assert _static_catalog(mgr, "paged") == catalog.items

        catalog.fill(range(1, 8))
        catalog.mode = "rejects-cursor"
        added, removed = mgr.refresh_sync("paged", timeout=15)["paged"] or ([], [])
        assert _static_catalog(mgr, "paged") == {
            kind: names[:2] for kind, names in catalog.items.items()
        }
    assert added == []
    assert removed == ["mcp__paged__tools_0", "mcp__paged__tools_3", "mcp__paged__tools_4"]


def test_unparseable_later_page_fails_the_walk() -> None:
    """A later page that fails the SDK's result validation fails the walk, unlike a JSON-RPC error.

    Keeping the pages before it would hide every entry from the malformed one onward behind a log
    line; the error names what to fix instead (#1224).
    """

    async def list_page(
        *, params: mcp_types.PaginatedRequestParams | None
    ) -> mcp_types.ListToolsResult:
        if params is None:
            return mcp_types.ListToolsResult(
                tools=[mcp_types.Tool(name="first", inputSchema={"type": "object"})],
                nextCursor="next",
            )
        return mcp_types.ListToolsResult.model_validate({"tools": "not a list"})

    with pytest.raises(InvalidCatalogError) as caught:
        asyncio.run(
            _list_catalog("srv", "tools", list_page, lambda page: page.tools, lambda t: t.name, 10)
        )
    assert str(caught.value) == (
        "MCP server 'srv' sent an invalid tools list at tools: "
        "Input should be a valid list (page 2)"
    )


# A stdio server paging five tools two at a time, which exits while answering a
# follow-up page once its flag file exists, so the transport dies mid-walk.
DYING_SERVER_SRC = textwrap.dedent(
    """
    import os, sys

    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    flag = sys.argv[1]
    names = [f"tools_{i}" for i in range(5)]
    server = Server("dying")


    async def list_tools(request):
        cursor = request.params.cursor if request.params is not None else None
        if cursor is not None and os.path.exists(flag):
            os._exit(0)
        start = int(cursor or 0)
        end = start + 2
        return types.ServerResult(
            types.ListToolsResult(
                tools=[types.Tool(name=n, inputSchema={"type": "object"}) for n in names[start:end]],
                nextCursor=str(end) if end < len(names) else None,
            )
        )


    server.request_handlers[types.ListToolsRequest] = list_tools


    async def main():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


    anyio.run(main)
    """
)


def test_transport_dying_mid_walk_fails_the_refresh(tmp_path: Path) -> None:
    """A dead transport is no answer: the refresh fails and the published catalog stays."""
    script = tmp_path / "dying_server.py"
    script.write_text(DYING_SERVER_SRC)
    flag = tmp_path / "die"
    config = {"type": "stdio", "command": sys.executable, "args": [str(script), str(flag)]}

    with _started_manager({"dying": config}) as mgr:
        complete = _static_catalog(mgr, "dying")["tools"]
        assert complete == [f"tools_{i}" for i in range(5)]

        flag.touch()
        assert mgr.refresh_sync("dying", timeout=15) == {"dying": None}
        assert _static_catalog(mgr, "dying")["tools"] == complete
