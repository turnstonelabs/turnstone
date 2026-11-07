"""A malformed catalog entry fails its MCP server with a clear error (#1224), and a tool whose input
schema is not valid JSON Schema is left out of its server's catalog.

The SDK validates a list result as a whole, so one malformed tool rejects its server's entire
catalog. A real low-level MCP server, served over streamable HTTP in this process, lists one valid
and one malformed tool. The tests connect to it through ``MCPClientManager`` on the static and the
per-user pool paths and check the error operators see, that the breaker records nothing, that the
static health loop retries on its slow cadence, and that a fixed server publishes its catalog.

The SDK accepts a tool whose input schema is an object, even when the schema is not valid JSON
Schema, and a provider that checks tool schemas then refuses every request that offers the tool.
Each path that publishes a catalog leaves such a tool out, publishes the server's other tools, and
lists the tool left out, with the reason, in the server's status and in the connection test.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import sys
import threading
import time
import warnings
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mcp_types
import pytest
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.routing import Mount

from tests.conftest import _free_port, _poll_until, _run_on_loop, _wait_tcp_ready, serve_until_exit
from turnstone.core import mcp_client
from turnstone.core.mcp_client import (
    InvalidCatalogError,
    MCPClientManager,
    SkippedTool,
    _AuthCapture,
    _decode_tool_result,
    _input_schema_problem,
    _invalid_page_error,
    _split_tools,
    probe_http_server,
)
from turnstone.core.oauth.context import OAuthContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

GOOD = {"name": "good", "inputSchema": {"type": "object", "properties": {}}}
NO_SCHEMA = {"name": "broken", "description": "lists no input schema"}
FIXED = {**NO_SCHEMA, "inputSchema": {"type": "object"}}
NO_SCHEMA_ERROR = (
    "MCP server 'srv' lists an invalid tool 'broken' at tools[1].inputSchema: Field required"
)
# The SDK accepts this tool, whose input schema is an object, but its properties' types are not
# JSON Schema types.
BAD_SCHEMA = {
    "name": "getOpportunities",
    "inputSchema": {
        "type": "object",
        "properties": {
            "Offset": {"type": ""},
            "Limit": {"type": ""},
            "ModifiedDate": {"type": "dateTime"},
        },
    },
}
REPAIRED = {
    "name": "getOpportunities",
    "inputSchema": {
        "type": "object",
        "properties": {
            "Offset": {"type": "integer"},
            "Limit": {"type": "integer"},
            "ModifiedDate": {"type": "string", "format": "date-time"},
        },
    },
}
# What a check says of a ``type`` that names no JSON Schema type.
NOT_A_TYPE = "is not one of ['array', 'boolean', 'integer', 'null', 'number', 'object', 'string']"
BAD_SCHEMA_SKIP = {
    "name": "getOpportunities",
    "reason": f"inputSchema.properties.Offset.type {NOT_A_TYPE} (2 more errors)",
}
OTHER_BAD_SCHEMA = {
    "name": "otherBroken",
    "inputSchema": {"type": "object", "properties": {"n": {"type": "dateTime"}}},
}
TUPLE_ITEMS = {
    "type": "object",
    "properties": {"a": {"type": "array", "items": [{"type": "string"}]}},
}
NAN_OR_INFINITY = (
    "inputSchema.properties.amount.maximum is NaN or an infinity, which JSON cannot carry"
)


class Upstream:
    """What the server lists, how it pages the list, and what a tool call returns."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = [GOOD, NO_SCHEMA]
        self.page_size = 10
        self.call_result: dict[str, Any] = {"content": [{"type": "text", "text": "ok"}]}
        self.list_requests = 0

    def page(self, cursor: str | None) -> dict[str, Any]:
        self.list_requests += 1
        start = int(cursor or 0)
        end = start + self.page_size
        page: dict[str, Any] = {"tools": self.tools[start:end]}
        if end < len(self.tools):
            page["nextCursor"] = str(end)
        return page


def _build_server(upstream: Upstream) -> Server[Any]:
    async def _raw_middleware(ctx: Any, call_next: Any) -> Any:
        # Short-circuiting without call_next sends the raw dict on the wire,
        # bypassing the server's outbound result validation so the client sees
        # the malformed catalog or tool result.
        if ctx.method == "tools/list":
            cursor = (ctx.params or {}).get("cursor")
            return {
                "resultType": "tools/list",
                "cacheScope": "public",
                "ttlMs": 0,
                **upstream.page(cursor),
            }
        if ctx.method == "tools/call":
            return {"resultType": "tools/call", **upstream.call_result}
        return await call_next(ctx)

    async def _noop_list(_ctx: Any, _params: Any) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=[])

    async def _noop_call(_ctx: Any, _params: Any) -> mcp_types.CallToolResult:
        return mcp_types.CallToolResult(content=[])

    server = Server(
        "upstream",
        on_list_tools=_noop_list,
        on_call_tool=_noop_call,
    )
    server.middleware.append(_raw_middleware)
    return server


@pytest.fixture
def upstream_server() -> Iterator[tuple[str, Upstream]]:
    upstream = Upstream()
    sessions = StreamableHTTPSessionManager(app=_build_server(upstream))

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
        target=serve_until_exit, args=(server,), daemon=True, name="invalid-catalog-upstream"
    )
    thread.start()
    try:
        assert _wait_tcp_ready(port, 5.0), "upstream MCP server did not come up"
        yield f"http://127.0.0.1:{port}/mcp", upstream
    finally:
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=5)


@contextlib.contextmanager
def _started_manager(servers: dict[str, Any], **attrs: float) -> Iterator[MCPClientManager]:
    """A started manager, with *attrs* (timing constants) set before it connects."""
    with patch(
        "turnstone.core.mcp_client.load_config",
        return_value={"static_health_check_seconds": 30},
    ):
        mgr = MCPClientManager(servers)
    for name, value in attrs.items():
        setattr(mgr, name, value)
    mgr.start()
    try:
        yield mgr
    finally:
        mgr.shutdown()


def _static(url: str) -> dict[str, Any]:
    return {"srv": {"type": "http", "url": url}}


def test_static_connect_reports_the_invalid_tool(upstream_server: tuple[str, Upstream]) -> None:
    url, upstream = upstream_server

    with _started_manager(_static(url)) as mgr:
        # Long enough for the health loop's first tick, which would retry at once unprompted.
        time.sleep(0.5)
        status = mgr.get_server_status("srv")
        retry_in = mgr._static_reconnect_next["srv"] - time.monotonic()
        published = mgr.is_mcp_tool("mcp__srv__good")

    assert status["connected"] is False
    assert status["error"] == NO_SCHEMA_ERROR
    assert status["consecutive_failures"] == 0
    assert status["circuit_open"] is False
    assert upstream.list_requests == 1
    assert retry_in > mgr._INVALID_CATALOG_RETRY_S - 10
    assert not published


def test_health_loop_retries_slowly_and_publishes_once_fixed(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    # A reconnect backoff this short would retry many times within the half second checked below.
    timing = {
        "_INVALID_CATALOG_RETRY_S": 1.0,
        "_STATIC_RECONNECT_BASE_S": 0.01,
        "_STATIC_RECONNECT_MAX_S": 0.05,
    }

    with _started_manager(_static(url), **timing) as mgr:
        assert _poll_until(lambda: upstream.list_requests == 2, 10), "the health loop never retried"
        time.sleep(0.5)
        assert upstream.list_requests == 2
        failing = mgr.get_server_status("srv")

        upstream.tools = [GOOD, FIXED]
        # Status reads connected while discovery is still running, so wait for the commit.
        assert _poll_until(lambda: mgr.is_mcp_tool("mcp__srv__broken"), 10)
        fixed = mgr.get_server_status("srv")

    assert failing["error"] == NO_SCHEMA_ERROR
    assert failing["consecutive_failures"] == 0
    assert fixed["connected"] is True
    assert fixed["tools"] == 2
    assert fixed["error"] == ""


def test_kept_tools_leave_after_a_reconnect_finds_an_invalid_catalog(
    upstream_server: tuple[str, Upstream],
) -> None:
    """A dispatch to a tool kept from the last good connect reconnects once, not on every call.

    The invalid catalog counts against no breaker, so the breaker cannot cap the reconnects; the
    server's earlier tools leave instead, and the health loop's slow retry brings them back.
    """
    url, upstream = upstream_server
    upstream.tools = [GOOD]

    async def _lose_session() -> None:
        async with mgr._static_connect_lock_for("srv"):
            await mgr._teardown_static_session("srv")

    with _started_manager(_static(url)) as mgr:
        assert mgr._loop is not None
        assert mgr.is_mcp_tool("mcp__srv__good")
        upstream.tools = [GOOD, NO_SCHEMA]
        _run_on_loop(mgr._loop, _lose_session())
        before = upstream.list_requests

        with pytest.raises(RuntimeError, match="reconnect failed: MCP server 'srv' lists"):
            mgr.call_tool_sync("mcp__srv__good", {}, timeout=10)
        with pytest.raises(ValueError, match="Unknown MCP tool"):
            mgr.call_tool_sync("mcp__srv__good", {}, timeout=10)
        reconnects = upstream.list_requests - before
        retry_in = mgr._static_reconnect_next["srv"] - time.monotonic()
        status = mgr.get_server_status("srv")

    assert reconnects == 1
    assert retry_in > mgr._INVALID_CATALOG_RETRY_S - 10
    assert status["error"] == NO_SCHEMA_ERROR
    assert status["consecutive_failures"] == 0


def test_operator_reconnect_schedules_the_slow_retry_and_success_clears_it(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD]

    with _started_manager(_static(url)) as mgr:
        upstream.tools = [GOOD, NO_SCHEMA]
        failed = mgr.reconnect_sync("srv")
        retry_in = mgr._static_reconnect_next["srv"] - time.monotonic()
        # As after many slow retries; a session lost right after the fix must not inherit it.
        mgr._static_reconnect_attempt["srv"] = 10
        upstream.tools = [GOOD, FIXED]
        fixed = mgr.reconnect_sync("srv")
        leftover = [
            name
            for name in ("_static_reconnect_next", "_static_reconnect_attempt")
            if "srv" in getattr(mgr, name)
        ]

    assert failed["error"] == NO_SCHEMA_ERROR
    assert retry_in > mgr._INVALID_CATALOG_RETRY_S - 10
    assert fixed["error"] == ""
    assert fixed["tools"] == 2
    assert leftover == []


def test_pool_connect_reports_an_invalid_tool_on_a_later_page(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.page_size = 2
    upstream.tools = [GOOD, {**GOOD, "name": "also_good"}, NO_SCHEMA]
    key = ("user-1", "srv")

    async def _connect() -> None:
        await mgr._connect_one_pool(
            key,
            {"type": "streamable-http", "url": url, "headers": {}},
            "access-token",
            auth_capture=_AuthCapture(),
        )

    with _started_manager({}) as mgr:
        assert mgr._loop is not None
        with pytest.raises(InvalidCatalogError) as caught:
            _run_on_loop(mgr._loop, _connect())
        published = mgr.is_mcp_tool("mcp__srv__good", user_id="user-1")
        failures = mgr._consecutive_failures.get("srv", 0)

    assert str(caught.value) == (
        "MCP server 'srv' lists an invalid tool 'broken' at tools[0].inputSchema: "
        "Field required (page 2)"
    )
    assert upstream.list_requests == 2
    assert not published
    assert failures == 0


def _prime_pool(mgr: MCPClientManager, url: str, user_id: str) -> None:
    """Register *url* as the per-user server ``srv`` and prime *user_id*'s pool for it."""
    row = {
        "name": "srv",
        "transport": "streamable-http",
        "url": url,
        "auth_type": "oauth_user",
        "enabled": True,
    }
    storage = MagicMock()
    storage.get_mcp_server_by_name.return_value = row
    lookup = AsyncMock(return_value=SimpleNamespace(kind="token", token="access-token"))
    mgr.set_storage(storage)
    mgr.set_app_state(SimpleNamespace(oauth_context=OAuthContext(token_store=MagicMock())))
    mgr._oauth_user_server_names = {"srv"}
    assert mgr._loop is not None
    with patch("turnstone.core.mcp_client.get_user_access_token_classified", new=lookup):
        _run_on_loop(mgr._loop, mgr._prime_user_pools(user_id))


def test_pool_prime_shows_the_invalid_tool_in_the_users_status(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, _upstream = upstream_server

    with _started_manager({}) as mgr:
        _prime_pool(mgr, url, "user-1")
        status = mgr.get_server_status("srv", user_id="user-1")

    assert status["discovery_error"] == NO_SCHEMA_ERROR
    assert status["tools"] == 0
    assert status["consecutive_failures"] == 0


def test_refresh_keeps_the_last_valid_catalog(upstream_server: tuple[str, Upstream]) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD]

    with _started_manager(_static(url)) as mgr:
        assert mgr.get_server_status("srv")["tools"] == 1
        upstream.tools = [GOOD, NO_SCHEMA]
        assert mgr.refresh_sync("srv", timeout=15) == {"srv": None}
        status = mgr.get_server_status("srv")
        # Retried like any failed refresh: the next full pass is what clears the error.
        retry_armed = "srv" in mgr._static_refresh_retry
        result = _decode_tool_result(mgr.call_tool_sync("mcp__srv__good", {}, timeout=10))

    assert status["connected"] is True
    assert status["tools"] == 1
    assert status["error"] == f"Refresh failed: InvalidCatalogError: {NO_SCHEMA_ERROR}"
    assert status["consecutive_failures"] == 0
    assert retry_armed
    assert result == "ok"


def test_invalid_call_result_counts_against_no_breaker(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD]
    upstream.call_result = {"content": [{"type": "text"}]}

    with _started_manager(_static(url)) as mgr:
        for _ in range(mgr._CB_FAILURE_THRESHOLD):
            with pytest.raises(ValidationError):
                mgr.call_tool_sync("mcp__srv__good", {}, timeout=10)
        status = mgr.get_server_status("srv")
        upstream.call_result = {"content": [{"type": "text", "text": "ok"}]}
        result = _decode_tool_result(mgr.call_tool_sync("mcp__srv__good", {}, timeout=10))

    assert status["connected"] is True
    assert status["consecutive_failures"] == 0
    assert status["circuit_open"] is False
    assert result == "ok"


def test_recursive_output_schema_counts_against_no_breaker(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD, {**GOOD, "name": "recursive", "outputSchema": {"$ref": "#"}}]
    upstream.call_result = {
        "content": [{"type": "text", "text": "ok"}],
        "structuredContent": {},
    }

    with _started_manager(_static(url)) as mgr:
        session = mgr._static_servers["srv"].session
        for _ in range(mgr._CB_FAILURE_THRESHOLD + 1):
            with pytest.raises(RecursionError):
                mgr.call_tool_sync("mcp__srv__recursive", {}, timeout=10)
        status = mgr.get_server_status("srv")
        assert mgr._static_servers["srv"].session is session
        result = _decode_tool_result(mgr.call_tool_sync("mcp__srv__good", {}, timeout=10))
        assert mgr._static_servers["srv"].session is session

    assert status["connected"] is True
    assert status["consecutive_failures"] == 0
    assert status["circuit_open"] is False
    assert result == "ok"


def _invalid(result_type: type[BaseModel], payload: dict[str, Any]) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        result_type.model_validate(payload)
    return caught.value


@pytest.mark.parametrize(
    ("result_type", "kind", "page", "payload", "message"),
    [
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [GOOD, NO_SCHEMA]},
            NO_SCHEMA_ERROR,
            id="missing-field-names-the-entry",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [{"inputSchema": {"type": "object"}}]},
            "MCP server 'srv' lists an invalid tool at tools[0].name: Field required",
            id="nameless-entry",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            3,
            {"tools": [{"name": 5}, {"name": "x", "inputSchema": []}]},
            "MCP server 'srv' lists an invalid tool at tools[0].name: "
            "Input should be a valid string (page 3; 2 more errors)",
            id="wrong-type-on-a-later-page",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [{"name": "line\nbreak"}]},
            "MCP server 'srv' lists an invalid tool 'line\\nbreak' at tools[0].inputSchema: "
            "Field required",
            id="name-is-escaped",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [{"name": "n" * 100}]},
            f"MCP server 'srv' lists an invalid tool '{'n' * 64}' at tools[0].inputSchema: "
            "Field required",
            id="long-name-is-cut",
        ),
        pytest.param(
            mcp_types.ListResourceTemplatesResult,
            "resource templates",
            1,
            {"resourceTemplates": [{"name": "files"}]},
            "MCP server 'srv' lists an invalid resource template 'files' at "
            "resourceTemplates[0].uriTemplate: Field required",
            id="other-catalog",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [GOOD], "nextCursor": 7},
            "MCP server 'srv' sent an invalid tools list at nextCursor: "
            "Input should be a valid string",
            id="page-level-field",
        ),
    ],
)
def test_error_names_the_entry_and_field(
    result_type: type[BaseModel], kind: str, page: int, payload: dict[str, Any], message: str
) -> None:
    error = _invalid_page_error("srv", kind, page, _invalid(result_type, payload))
    assert str(error) == message
    # A copy rebuilds the exception from its message alone, as pickling does.
    assert str(copy.copy(error)) == message


# ---------------------------------------------------------------------------
# A tool whose input schema is not valid JSON Schema is left out of the catalog
# ---------------------------------------------------------------------------


def _nested(depth: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(depth):
        schema = {"type": "object", "properties": {"x": schema}}
    return schema


def _left_out_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The warnings that name tools a catalog leaves out."""
    return [r.getMessage() for r in caplog.records if "left out of its catalog" in r.getMessage()]


def _count_checks(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The schemas checked from now on, with no verdict remembered from before."""
    checked: list[dict[str, Any]] = []
    check = mcp_client._input_schema_problem

    def _counting(schema: dict[str, Any]) -> str | None:
        checked.append(schema)
        return check(schema)

    monkeypatch.setattr(mcp_client, "_input_schema_problem", _counting)
    monkeypatch.setattr(mcp_client, "_INPUT_SCHEMA_VERDICTS", {})
    return checked


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        pytest.param(REPAIRED["inputSchema"], None, id="valid"),
        pytest.param(BAD_SCHEMA["inputSchema"], BAD_SCHEMA_SKIP["reason"], id="three-wrong-types"),
        pytest.param(
            {"type": "object", "properties": {"n": {"type": "string", "required": True}}},
            "inputSchema.properties.n.required is not of type 'array'",
            id="boolean-required",
        ),
        pytest.param(
            {"type": "object", "properties": {"when": {"type": "timestamp-with-zone"}}},
            f"inputSchema.properties.when.type {NOT_A_TYPE}",
            id="value-left-out",
        ),
        pytest.param(
            # Each of the 2020-12 metaschema's vocabularies reports a non-schema once.
            TUPLE_ITEMS,
            "inputSchema.properties.a.items is not of type 'object', 'boolean'",
            id="one-error-counted-once",
        ),
        pytest.param(
            {"$schema": "http://json-schema.org/draft-07/schema#", **TUPLE_ITEMS},
            None,
            id="declared-dialect",
        ),
        pytest.param(
            # Draft-03 takes any string as a type name; the schema is checked as 2020-12.
            {"$schema": "http://json-schema.org/draft-03/schema#", **BAD_SCHEMA["inputSchema"]},
            BAD_SCHEMA_SKIP["reason"],
            id="earlier-draft-checked-as-2020-12",
        ),
        pytest.param(
            # Draft-04 wants at least one name in ``required``; 2020-12, and providers, do not.
            {
                "$schema": "http://json-schema.org/draft-04/schema#",
                "type": "object",
                "properties": {},
                "required": [],
            },
            None,
            id="draft-04-checked-as-2020-12",
        ),
        pytest.param(
            # A Unicode property class: valid ECMA-262, an error to Python's re.
            {"type": "object", "properties": {"n": {"type": "string", "pattern": "^\\p{L}+$"}}},
            None,
            id="pattern-python-cannot-compile",
        ),
        pytest.param(
            {"$schema": "https://example.com/dialect", "type": "object"},
            None,
            id="unknown-dialect",
        ),
        pytest.param({"$schema": "http://[::1", "type": "object"}, None, id="unsplittable-dialect"),
        pytest.param(
            {"$schema": 7, "type": "object"},
            "inputSchema.$schema is not of type 'string'",
            id="dialect-not-a-string",
        ),
        pytest.param(
            {"type": "object", "properties": {"line\nbreak": {"type": "text"}}},
            f"inputSchema.properties.line break.type {NOT_A_TYPE}",
            id="one-line",
        ),
        pytest.param(_nested(150), "inputSchema is nested too deeply to check", id="too-deep"),
        pytest.param(
            {"type": "object", "allOf": [{"type": "x"}]},
            f"inputSchema.allOf[0].type {NOT_A_TYPE}",
            id="index-in-path",
        ),
        pytest.param(
            {"type": "object", "properties": {"amount": {"enum": [1, float("nan")]}}},
            "inputSchema.properties.amount.enum[1] is NaN or an infinity, which JSON cannot carry",
            id="nan-at-an-index",
        ),
    ],
)
def test_input_schema_problem_names_the_field_and_never_its_value(
    schema: dict[str, Any], problem: str | None
) -> None:
    with warnings.catch_warnings():
        # No dialect, named or not, makes the check warn.
        warnings.simplefilter("error")
        assert _input_schema_problem(schema) == problem


def test_a_long_path_keeps_the_words_and_the_field_they_are_about() -> None:
    """A schema generated from a deep API description can have a path longer than the line."""
    schema: dict[str, Any] = {"type": "dateTime"}
    for level in range(8):
        schema = {"type": "object", "properties": {f"section_{level}_" + "x" * 30: schema}}

    reason = _input_schema_problem(schema)

    assert reason is not None
    assert len(reason) <= 256
    assert reason.startswith("inputSchema...")
    assert reason.endswith(f".section_0_{'x' * 30}.type {NOT_A_TYPE}")


@pytest.mark.parametrize(
    ("uri", "tuple_items_valid"),
    [
        pytest.param("http://json-schema.org/draft-06/schema#", True, id="draft-06"),
        pytest.param("https://json-schema.org/draft-07/schema", True, id="draft-07-https"),
        pytest.param("http://json-schema.org/draft/2019-09/schema#", True, id="2019-09-http"),
        pytest.param("https://json-schema.org/draft/2020-12/schema", False, id="2020-12"),
        pytest.param("http://json-schema.org/draft-04/schema#", False, id="draft-04"),
        pytest.param("HTTP://json-schema.org/draft-07/schema#", True, id="scheme-in-capitals"),
        pytest.param(
            " http://json-schema.org/draft-07/schema#" + chr(10), True, id="surrounding-space"
        ),
        # Without a scheme it is not a URI, so it names no dialect.
        pytest.param("json-schema.org/draft-07/schema", False, id="no-scheme"),
    ],
)
def test_a_declared_dialect_is_honored_from_draft_06_on_under_either_scheme(
    uri: str, tuple_items_valid: bool
) -> None:
    """``items`` may be a list of schemas up to 2019-09; 2020-12 moved that to ``prefixItems``."""
    problem = _input_schema_problem({"$schema": uri, **TUPLE_ITEMS})

    assert (problem is None) is tuple_items_valid


@pytest.mark.parametrize("bound", ["1e400", "Infinity", "-Infinity", "NaN"])
def test_a_number_json_cannot_carry_leaves_the_tool_out(bound: str) -> None:
    """The SDK reads these from a listing, and the provider SDKs refuse to send a request holding
    one."""
    listing = (
        '{"tools": [{"name": "quote", "inputSchema": {"type": "object", "properties": '
        f'{{"amount": {{"type": "number", "maximum": {bound}}}}}}}}}]}}'
    )
    (tool,) = mcp_types.ListToolsResult.model_validate_json(listing).tools

    published, skipped = _split_tools([tool])

    assert published == []
    assert skipped == (SkippedTool("quote", NAN_OR_INFINITY),)
    # The check itself says so, not only the catalog that remembers its verdicts.
    assert _input_schema_problem(tool.input_schema) == NAN_OR_INFINITY


def test_verdicts_are_kept_by_content_and_cleared_when_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = _count_checks(monkeypatch)
    monkeypatch.setattr(mcp_client, "_INPUT_SCHEMA_VERDICTS_MAX", 2)

    first = mcp_client._cached_input_schema_problem(BAD_SCHEMA["inputSchema"])
    again = mcp_client._cached_input_schema_problem(copy.deepcopy(BAD_SCHEMA["inputSchema"]))
    mcp_client._cached_input_schema_problem({"type": "object"})
    mcp_client._cached_input_schema_problem(REPAIRED["inputSchema"])

    assert first == again == BAD_SCHEMA_SKIP["reason"]
    assert len(checked) == 3
    assert list(mcp_client._INPUT_SCHEMA_VERDICTS.values()) == [None]


def test_a_schema_too_deep_to_check_is_checked_again_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """That verdict depends on how deep the caller's own stack is, not only on the schema."""
    checked = _count_checks(monkeypatch)

    first = mcp_client._cached_input_schema_problem(_nested(150))
    again = mcp_client._cached_input_schema_problem(_nested(150))

    assert first == again == "inputSchema is nested too deeply to check"
    assert len(checked) == 2
    assert mcp_client._INPUT_SCHEMA_VERDICTS == {}


@pytest.mark.no_cover
def test_a_remembered_verdict_keeps_the_gil_while_another_thread_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hashlib hands off the GIL for an update of 2 KiB or more, and while another thread is busy
    each hand-off costs the caller up to a switch interval: 300 lookups of a large schema would take
    300 of them. Fed less at a time, the digest keeps the GIL, and the lookups share the interpreter
    with the busy thread."""
    monkeypatch.setattr(mcp_client, "_INPUT_SCHEMA_VERDICTS", {})
    schema = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string", "description": "x" * 40} for i in range(150)},
    }
    assert mcp_client._cached_input_schema_problem(schema) is None
    stop = threading.Event()

    def _spin() -> None:
        while not stop.is_set():
            pass

    busy = threading.Thread(target=_spin, name="test-busy")
    busy.start()
    try:
        started = time.perf_counter()
        for _ in range(300):
            mcp_client._cached_input_schema_problem(schema)
        elapsed = time.perf_counter() - started
    finally:
        stop.set()
        busy.join()

    assert elapsed < 300 * sys.getswitchinterval() / 2


def test_a_tool_left_out_is_named_on_one_bounded_line() -> None:
    tool = mcp_types.Tool(
        name="get\nOpportunities" + "x" * 200, input_schema=BAD_SCHEMA["inputSchema"]
    )

    published, skipped = _split_tools([tool])

    assert published == []
    assert skipped == (SkippedTool("get Opportunities" + "x" * 111, BAD_SCHEMA_SKIP["reason"]),)


def test_the_warning_names_ten_tools_left_out_and_counts_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mgr = MCPClientManager({})
    state = SimpleNamespace(tools=[], skipped_tools=())
    skipped = tuple(SkippedTool(f"t{i}", "why") for i in range(12))

    with caplog.at_level(logging.WARNING, logger="turnstone.mcp"):
        mgr._publish_tool_catalog(state, "srv", [], skipped)  # type: ignore[arg-type]

    assert state.skipped_tools == skipped
    (logged,) = _left_out_warnings(caplog)
    # A log renderer may show the arguments apart from the words; each check holds either way.
    assert "'t9' (why)" in logged
    assert "; and 2 more" in logged
    assert "'t10'" not in logged


def test_static_connect_leaves_out_a_tool_whose_input_schema_is_invalid(
    upstream_server: tuple[str, Upstream], caplog: pytest.LogCaptureFixture
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD, BAD_SCHEMA]

    with (
        caplog.at_level(logging.WARNING, logger="turnstone.mcp"),
        _started_manager(_static(url)) as mgr,
    ):
        status = mgr.get_server_status("srv")
        published = [mgr.is_mcp_tool(f"mcp__srv__{name}") for name in ("good", "getOpportunities")]

    assert status["connected"] is True
    assert status["error"] == ""
    assert status["tools"] == 1
    assert status["skipped_tools"] == [BAD_SCHEMA_SKIP]
    assert published == [True, False]
    (logged,) = _left_out_warnings(caplog)
    assert "getOpportunities" in logged
    assert "[user=" not in logged


def test_a_warning_names_the_tools_left_out_only_when_they_change(
    upstream_server: tuple[str, Upstream], caplog: pytest.LogCaptureFixture
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD, BAD_SCHEMA]

    with (
        caplog.at_level(logging.WARNING, logger="turnstone.mcp"),
        _started_manager(_static(url)) as mgr,
    ):
        after_connect = len(_left_out_warnings(caplog))
        mgr.refresh_sync("srv", timeout=15)
        after_same = len(_left_out_warnings(caplog))
        upstream.tools = [GOOD, REPAIRED]
        mgr.refresh_sync("srv", timeout=15)
        after_repair = len(_left_out_warnings(caplog))
        upstream.tools = [GOOD, BAD_SCHEMA]
        mgr.refresh_sync("srv", timeout=15)
        after_breaking_again = len(_left_out_warnings(caplog))

    assert (after_connect, after_same, after_repair, after_breaking_again) == (1, 1, 1, 2)


def test_the_warning_ignores_order_and_comes_again_for_a_server_added_again(
    upstream_server: tuple[str, Upstream], caplog: pytest.LogCaptureFixture
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD, BAD_SCHEMA, OTHER_BAD_SCHEMA]

    with (
        caplog.at_level(logging.WARNING, logger="turnstone.mcp"),
        _started_manager(_static(url)) as mgr,
    ):
        after_connect = len(_left_out_warnings(caplog))
        upstream.tools = [GOOD, OTHER_BAD_SCHEMA, BAD_SCHEMA]
        mgr.refresh_sync("srv", timeout=15)
        after_reorder = len(_left_out_warnings(caplog))
        assert mgr.remove_server_sync("srv")
        mgr.add_server_sync("srv", _static(url)["srv"])
        after_added_again = len(_left_out_warnings(caplog))

    assert (after_connect, after_reorder, after_added_again) == (1, 1, 2)


def test_each_user_of_a_per_user_server_is_warned_once(
    upstream_server: tuple[str, Upstream], caplog: pytest.LogCaptureFixture
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD, BAD_SCHEMA]

    async def _refresh(user_id: str) -> tuple[list[str], list[str]]:
        key = (user_id, "srv")
        async with mgr._user_pool_entries[key].open_lock:
            return await mgr._refresh_pool_server_tools(key)

    with (
        caplog.at_level(logging.WARNING, logger="turnstone.mcp"),
        _started_manager({}) as mgr,
    ):
        _prime_pool(mgr, url, "user-1")
        _prime_pool(mgr, url, "user-2")
        after_primes = len(_left_out_warnings(caplog))
        assert mgr._loop is not None
        for user_id in ("user-1", "user-2", "user-1", "user-2"):
            _run_on_loop(mgr._loop, _refresh(user_id))
        warnings = _left_out_warnings(caplog)

    assert (after_primes, len(warnings)) == (2, 2)
    # Each names the user whose catalog it is.
    assert "[user=user-1]" in warnings[0]
    assert "[user=user-2]" in warnings[1]


def test_refresh_leaves_out_a_broken_tool_and_publishes_it_once_repaired(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD]

    with _started_manager(_static(url)) as mgr:
        upstream.tools = [GOOD, BAD_SCHEMA]
        broke = mgr.refresh_sync("srv", timeout=15)
        broken = mgr.get_server_status("srv")
        upstream.tools = [GOOD, REPAIRED]
        repaired_refresh = mgr.refresh_sync("srv", timeout=15)
        repaired = mgr.get_server_status("srv")
        published = mgr.is_mcp_tool("mcp__srv__getOpportunities")

    assert broke == {"srv": ([], [])}
    assert broken["tools"] == 1
    assert broken["skipped_tools"] == [BAD_SCHEMA_SKIP]
    assert broken["error"] == ""
    assert repaired_refresh == {"srv": (["mcp__srv__getOpportunities"], [])}
    assert repaired["tools"] == 2
    assert repaired["skipped_tools"] == []
    assert published


def test_an_invalid_catalog_withdraws_the_tools_left_out_with_the_rest(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD, BAD_SCHEMA]

    with _started_manager(_static(url)) as mgr:
        before = mgr._static_servers["srv"].skipped_tools
        upstream.tools = [GOOD, BAD_SCHEMA, NO_SCHEMA]
        mgr.reconnect_sync("srv")
        status = mgr.get_server_status("srv")
        after = mgr._static_servers["srv"].skipped_tools

    assert before == (SkippedTool(**BAD_SCHEMA_SKIP),)
    assert status["error"] == NO_SCHEMA_ERROR.replace("tools[1]", "tools[2]")
    assert status["skipped_tools"] == []
    assert after == ()


def test_status_lists_the_tools_left_out_only_while_connected(
    upstream_server: tuple[str, Upstream],
) -> None:
    """A lost session keeps its catalog for the reconnect; the status counts neither its tools nor
    the tools it leaves out until the server is connected again."""
    url, upstream = upstream_server
    upstream.tools = [GOOD, BAD_SCHEMA]

    async def _lose_session() -> None:
        async with mgr._static_connect_lock_for("srv"):
            await mgr._teardown_static_session("srv")

    with _started_manager(_static(url)) as mgr:
        assert mgr._loop is not None
        _run_on_loop(mgr._loop, _lose_session())
        status = mgr.get_server_status("srv")
        kept = mgr._static_servers["srv"].skipped_tools

    assert kept == (SkippedTool(**BAD_SCHEMA_SKIP),)
    assert status["connected"] is False
    assert status["tools"] == 0
    assert status["skipped_tools"] == []


def test_pool_catalog_leaves_out_the_tool_and_a_refresh_publishes_it_once_repaired(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD, BAD_SCHEMA]
    key = ("user-1", "srv")

    async def _refresh() -> tuple[list[str], list[str]]:
        async with mgr._user_pool_entries[key].open_lock:
            return await mgr._refresh_pool_server_tools(key)

    with _started_manager({}) as mgr:
        _prime_pool(mgr, url, "user-1")
        own = mgr.get_server_status("srv", user_id="user-1")
        aggregate = mgr.get_server_status("srv", aggregate=True)
        other = mgr.get_server_status("srv", user_id="user-2")
        published = [
            mgr.is_mcp_tool(f"mcp__srv__{name}", user_id="user-1")
            for name in ("good", "getOpportunities")
        ]
        upstream.tools = [GOOD, REPAIRED]
        assert mgr._loop is not None
        refreshed = _run_on_loop(mgr._loop, _refresh())
        repaired = mgr.get_server_status("srv", user_id="user-1")

    assert own["connected"] is True
    assert own["discovery_error"] == ""
    assert own["tools"] == 1
    assert own["skipped_tools"] == [BAD_SCHEMA_SKIP]
    assert aggregate["skipped_tools"] == [BAD_SCHEMA_SKIP]
    # Another user's status shows nothing of this user's pool.
    assert other["skipped_tools"] == []
    assert published == [True, False]
    assert refreshed == (["mcp__srv__getOpportunities"], [])
    assert repaired["tools"] == 2
    assert repaired["skipped_tools"] == []


def test_connection_test_names_the_tools_a_save_would_leave_out(
    upstream_server: tuple[str, Upstream], caplog: pytest.LogCaptureFixture
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD, BAD_SCHEMA]

    with caplog.at_level(logging.WARNING, logger="turnstone.mcp"):
        probe = probe_http_server(url, name="srv")

    assert probe.failure is None
    assert probe.tools == ["good"]
    assert probe.skipped_tools == (SkippedTool(**BAD_SCHEMA_SKIP),)
    # The test publishes nothing, so it logs no catalog.
    assert _left_out_warnings(caplog) == []
