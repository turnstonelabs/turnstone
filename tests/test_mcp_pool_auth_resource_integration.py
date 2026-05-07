"""Phase 7b integration tests — real-transport resource read 401/403/etc.

Mirror of :mod:`tests.test_mcp_pool_auth_integration` for the resource
path (RFC §3.2). Drives through the real ``streamablehttp_client``,
real httpx response-hook plumbing, and a real upstream subprocess
(``FastMCP`` with a programmable ``BehaviorMiddleware``). Direct
``httpx.HTTPStatusError`` injection is forbidden (invariant 14).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

from tests.conftest import make_mcp_token_cipher
from turnstone.core.mcp_client import MCPClientManager
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.mcp_oauth import TokenLookupResult
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.responses import Response

logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)


class BehaviorMiddleware(BaseHTTPMiddleware):
    """Programmable upstream behaviour — see
    :mod:`tests.test_mcp_pool_auth_integration` for the semantics. This
    copy serves the resource integration tests.
    """

    def __init__(self, app: Any, behaviour: dict[str, Any]) -> None:
        super().__init__(app)
        self._behaviour = behaviour

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        from starlette.responses import Response as StarletteResponse

        if request.method == "POST" and "/mcp" in str(request.url):
            self._behaviour.setdefault("post_auth_headers", []).append(
                request.headers.get("authorization")
            )

        mode = self._behaviour.get("mode", "never")
        if mode == "once_401":
            if not self._behaviour.get("_fired"):
                self._behaviour["_fired"] = True
                return StarletteResponse(
                    "unauthorized",
                    status_code=401,
                    headers={
                        "www-authenticate": self._behaviour.get(
                            "www_authenticate", 'Bearer error="invalid_token"'
                        )
                    },
                )
        elif mode == "always_401":
            return StarletteResponse(
                "unauthorized",
                status_code=401,
                headers={
                    "www-authenticate": self._behaviour.get(
                        "www_authenticate", 'Bearer error="invalid_token"'
                    )
                },
            )
        elif mode == "once_403_insufficient":
            if not self._behaviour.get("_fired"):
                self._behaviour["_fired"] = True
                return StarletteResponse(
                    "forbidden",
                    status_code=403,
                    headers={
                        "www-authenticate": self._behaviour.get(
                            "www_authenticate",
                            'Bearer error="insufficient_scope", scope="files:read"',
                        )
                    },
                )
        elif mode == "once_403_generic" and not self._behaviour.get("_fired"):
            self._behaviour["_fired"] = True
            return StarletteResponse(
                "forbidden",
                status_code=403,
                headers={
                    "www-authenticate": self._behaviour.get("www_authenticate", "Bearer realm=mcp")
                },
            )
        return await call_next(request)


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_server(port: int, behaviour: dict[str, Any]) -> uvicorn.Server:
    mcp = FastMCP(name="phase7b-resource-target", streamable_http_path="/mcp")

    @mcp.resource("res://hello")
    def hello() -> str:
        return "world"

    @mcp.resource("res://json/data")
    def jdata() -> str:
        return '{"k": 1}'

    # Echo tool exists so the e2e test can trigger ``_connect_one_pool``
    # (and the full tool + resource + prompt discovery) via prefix-parsed
    # ``call_tool_sync`` BEFORE the resource read. The other tests in this
    # module use ``_seed_pool_resource_map`` and never invoke tools, so
    # adding the tool is invisible to them.
    @mcp.tool()
    async def echo(payload: str = "default") -> str:
        return f"echoed:{payload}"

    app = mcp.streamable_http_app()
    app.add_middleware(BehaviorMiddleware, behaviour=behaviour)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    return uvicorn.Server(config)


def _wait_ready(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"upstream at 127.0.0.1:{port} not ready after {timeout}s")


@pytest.fixture
def upstream():
    port = _find_free_port()
    behaviour: dict[str, Any] = {}
    server = _build_server(port, behaviour)

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=_run, daemon=True, name="phase7b-resource-upstream")
    t.start()
    try:
        _wait_ready(port)
        yield f"http://127.0.0.1:{port}/mcp", behaviour
    finally:
        server.should_exit = True
        t.join(timeout=5)


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "test.db"))


def _seed_oauth_server(
    storage: SQLiteBackend,
    *,
    name: str = "pool-srv",
    server_id: str = "srv-pool",
    url: str = "https://mcp.example.com/sse",
) -> None:
    storage.create_mcp_server(
        server_id=server_id,
        name=name,
        transport="streamable-http",
        url=url,
        auth_type="oauth_user",
        oauth_client_id="client-abc",
        oauth_scopes="openid",
        oauth_audience=url,
    )


def _seed_user_token(
    storage: SQLiteBackend,
    cipher: Any,
    *,
    user_id: str = "user-1",
    server_name: str = "pool-srv",
    expires_in_seconds: int = 3600,
    access_token: str = "access-aaa",
    refresh_token: str | None = "refresh-rrr",
) -> None:
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    store = MCPTokenStore(storage, cipher, node_id="test")
    store.create_user_token(
        user_id,
        server_name,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes="openid",
        as_issuer="https://as.example.com",
        audience="https://mcp.example.com",
    )


def _make_app_state(storage: SQLiteBackend, *, cipher: Any) -> SimpleNamespace:
    return SimpleNamespace(
        auth_storage=storage,
        mcp_token_store=MCPTokenStore(storage, cipher, node_id="test"),
        mcp_oauth_http_client=MagicMock(),
        mcp_oauth_refresh_locks={},
        mcp_oauth_metadata_cache={},
    )


@pytest.fixture
def running_loop_mgr():
    cfg: dict[str, Any] = {}
    mgr = MCPClientManager(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="mcp-pool-test-loop")
    thread.start()
    mgr._loop = loop
    try:
        yield mgr, loop, thread
    finally:

        async def _drain(m: MCPClientManager) -> None:
            task = m._user_pool_eviction_task
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                m._user_pool_eviction_task = None

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_drain(mgr), loop).result(timeout=2)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def _seed_pool_resource_map(
    mgr: MCPClientManager, user_id: str, server_name: str, uri: str
) -> None:
    """Pre-seed ``_user_resource_map`` so ``_resolve_pool_target_resource``
    finds the URI. Production wires this through ``_connect_one_pool``;
    the integration tests seed it directly so the test focuses on the
    dispatch behaviour after resolution succeeds.
    """

    async def _seed() -> None:
        entry = await mgr._ensure_pool_entry((user_id, server_name))
        entry.resources = [
            {
                "uri": uri,
                "name": "",
                "description": "",
                "mimeType": "",
                "server": server_name,
            }
        ]
        mgr._rebuild_user_resource_map(user_id)

    assert mgr._loop is not None
    asyncio.run_coroutine_threadsafe(_seed(), mgr._loop).result(timeout=5)


# ---------------------------------------------------------------------------
# I-RP-1: 401 → refresh → retry → success (resource path)
# ---------------------------------------------------------------------------


def test_resource_read_401_refresh_and_retry_succeeds(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Real upstream returns 401 once, then 200. Carrier captures 401,
    force_refresh=True mints a new bearer, retry returns the resource.
    Hard invariant 3: breaker counter remains 0.
    """
    url, behaviour = upstream
    behaviour["mode"] = "once_401"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_resource_map(mgr, "user-1", "pool-srv", "res://hello")

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="token", token="refreshed-bearer")
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ):
        result = mgr.read_resource_sync("res://hello", user_id="user-1", timeout=15)

    assert result == "world"
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0
    post_headers = behaviour.get("post_auth_headers", [])
    assert len(post_headers) >= 2, f"expected >=2 POSTs; got {len(post_headers)}"
    assert post_headers[0] != post_headers[1], (
        "retry attached the same bearer as the initial; the dispatcher "
        "did not pick up the refreshed token."
    )
    entry = mgr._user_pool_entries[("user-1", "pool-srv")]
    assert entry.session is not None


# ---------------------------------------------------------------------------
# I-RP-2: persistent 401 → mcp_consent_required (resource path)
# ---------------------------------------------------------------------------


def test_resource_read_persistent_401_emits_consent_required(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, behaviour = upstream
    behaviour["mode"] = "always_401"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_resource_map(mgr, "user-1", "pool-srv", "res://hello")

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="token", token="refreshed-bearer")
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ), pytest.raises(RuntimeError) as exc_info:
        mgr.read_resource_sync("res://hello", user_id="user-1", timeout=15)

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_consent_required"
    assert payload["error"]["server"] == "pool-srv"
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# I-RP-3: 403 + insufficient_scope → mcp_insufficient_scope (resource path)
# ---------------------------------------------------------------------------


def test_resource_read_403_insufficient_scope_emits_structured_error(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, behaviour = upstream
    behaviour["mode"] = "once_403_insufficient"
    behaviour["www_authenticate"] = 'Bearer error="insufficient_scope", scope="files:read"'

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_resource_map(mgr, "user-1", "pool-srv", "res://hello")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ), pytest.raises(RuntimeError) as exc_info:
        mgr.read_resource_sync("res://hello", user_id="user-1", timeout=15)

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_insufficient_scope"
    assert payload["error"]["scopes_required"] == ["files:read"]
    post_headers = behaviour.get("post_auth_headers", [])
    assert len(post_headers) == 1, (
        f"403 must NOT trigger a retry; observed {len(post_headers)} POSTs"
    )
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# I-RP-3b: 403 generic → mcp_resource_read_forbidden
# ---------------------------------------------------------------------------


def test_resource_read_403_generic_forbidden(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, behaviour = upstream
    behaviour["mode"] = "once_403_generic"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_resource_map(mgr, "user-1", "pool-srv", "res://hello")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ), pytest.raises(RuntimeError) as exc_info:
        mgr.read_resource_sync("res://hello", user_id="user-1", timeout=15)

    payload = json.loads(str(exc_info.value))
    # Per the kind="resource" wiring of `_handle_auth_403`, the
    # operation-specific code surfaces here rather than the tool path's
    # generic mcp_tool_call_forbidden.
    assert payload["error"]["code"] == "mcp_resource_read_forbidden"
    assert "scopes_required" not in payload["error"]


# ---------------------------------------------------------------------------
# I-RP-6: breaker isolation — auth failures NEVER trip the breaker
# ---------------------------------------------------------------------------


def test_resource_read_breaker_unaffected_by_auth_failures(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Repeated 401 + refresh-failed cycles leave breaker at 0
    (hard invariant 3 verified end-to-end for the resource path)."""
    url, behaviour = upstream
    behaviour["mode"] = "always_401"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_resource_map(mgr, "user-1", "pool-srv", "res://hello")

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="refresh_failed")
        return TokenLookupResult(kind="token", token="access-aaa")

    # Re-seed each iteration: symmetric eviction (Phase 7b) clears
    # ``_user_resource_map`` on auth failure so the next dispatch's
    # resolver would miss without a fresh seed. Production reconnect
    # repopulates this; the test simulates that out-of-band.
    for _ in range(10):
        _seed_pool_resource_map(mgr, "user-1", "pool-srv", "res://hello")
        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ), pytest.raises(RuntimeError) as exc_info:
            mgr.read_resource_sync("res://hello", user_id="user-1", timeout=15)
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_consent_required"
        assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# Negative tests — token lookup edge cases (resource path)
# ---------------------------------------------------------------------------


def test_resource_read_missing_token_emits_consent_required(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, _behaviour = upstream
    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_resource_map(mgr, "user-1", "pool-srv", "res://hello")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="missing")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ), pytest.raises(RuntimeError) as exc_info:
        mgr.read_resource_sync("res://hello", user_id="user-1", timeout=10)

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_consent_required"


def test_resource_read_decrypt_failure_emits_token_undecryptable(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, _behaviour = upstream
    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_resource_map(mgr, "user-1", "pool-srv", "res://hello")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="decrypt_failure")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ), pytest.raises(RuntimeError) as exc_info:
        mgr.read_resource_sync("res://hello", user_id="user-1", timeout=10)

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_token_undecryptable_key_unknown"


def test_resource_read_http_url_emits_url_insecure(
    running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """An ``http://`` (non-loopback) oauth_user URL must surface
    ``mcp_oauth_url_insecure`` BEFORE the bearer is attached.
    """
    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url="http://example.com/mcp")
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_resource_map(mgr, "user-1", "pool-srv", "res://hello")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ), pytest.raises(RuntimeError) as exc_info:
        mgr.read_resource_sync("res://hello", user_id="user-1", timeout=5)

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_oauth_url_insecure"


def test_resource_read_unknown_uri_raises_value_error(
    running_loop_mgr: Any,
) -> None:
    """When the URI doesn't resolve to either pool or static, the
    static-path code raises ``ValueError``. Per-user-first resolution
    (scope decision 0.1) means user_id-bearing callers still hit this
    path when their pool catalog doesn't carry the URI."""
    mgr, _loop, _ = running_loop_mgr
    with pytest.raises(ValueError, match="Unknown MCP resource"):
        mgr.read_resource_sync("res://nonexistent", user_id="user-1", timeout=5)


# ---------------------------------------------------------------------------
# I-RP-E2E: real discovery + dispatch in same connect (no _seed_pool_resource_map)
# ---------------------------------------------------------------------------


def test_resource_read_e2e_discovery_then_dispatch_succeeds(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Drive REAL discovery + dispatch end-to-end through the pool path.

    Mirror of the tool path's
    ``test_integration_pool_reuse_401_refresh_and_retry_succeeds``: skips
    the ``_seed_pool_resource_map`` shortcut and lets ``_connect_one_pool``
    populate ``_user_resource_map`` from the real ``resources/list``
    upstream response. Verifies that the entry's discovered resources
    match what the FastMCP fixture advertises AND that
    ``_user_resource_map[user_id]`` is populated with the URI(s) after
    discovery — proving the discovery path actually fired.

    Resource URIs do NOT carry a server-name prefix (unlike tools and
    prompts), so the resource resolver cannot derive (server, uri) by
    parsing alone. The test triggers the connect via a prefix-parsed
    ``call_tool_sync`` first (which runs the full
    tools+resources+prompts discovery against the FastMCP fixture),
    then drives ``read_resource_sync`` against a URI that the
    upstream advertised — proving that real discovery wired the URI
    into the per-user catalog.

    Structural gate against a regression where resource discovery is
    silently skipped (e.g., a capability-gating bug that drops the
    ``resources/list`` call but keeps the connect succeeding).
    """
    url, behaviour = upstream
    behaviour["mode"] = "never"  # passthrough — discovery + dispatch both succeed

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    # NB: no `_seed_pool_resource_map` — the connect runs the real
    # ``resources/list`` against the FastMCP fixture and populates the
    # per-user catalog. The tool call below triggers that connect because
    # ``_resolve_pool_target`` derives (server, original) from the
    # ``mcp__pool-srv__echo`` prefix and lazy-connects via
    # ``_connect_one_pool``.

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ):
        # Step 1: trigger the connect via prefix-parsed tool dispatch.
        # Discovery (tools + resources + prompts) populates the per-user
        # catalogs.
        tool_result = mgr.call_tool_sync(
            "mcp__pool-srv__echo", {"payload": "ignite"}, user_id="user-1", timeout=15
        )
        assert "echoed:ignite" in tool_result

        # Step 2: now that discovery has populated ``_user_resource_map``,
        # the resource resolver finds ``res://hello`` and dispatches the
        # read on the SAME pool entry / session.
        result = mgr.read_resource_sync("res://hello", user_id="user-1", timeout=15)

    assert result == "world"
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0

    # Discovery populated the entry's resources with both fixtures
    # (``res://hello`` and ``res://json/data``) — proves real
    # ``resources/list`` ran during the connect, not just the targeted
    # ``resources/read``.
    entry = mgr._user_pool_entries[("user-1", "pool-srv")]
    assert entry.session is not None
    assert entry.resources is not None
    discovered_uris = {r["uri"] for r in entry.resources if not r.get("template")}
    assert "res://hello" in discovered_uris
    assert "res://json/data" in discovered_uris

    # ``_rebuild_user_resource_map`` ran during the connect, populating
    # the per-user catalog. This is the signal that discovery wired into
    # the routing tables — without it, ``read_resource_sync`` would have
    # raised ValueError because the resolver had no entry for the URI.
    user_resource_map = mgr._user_resource_map.get("user-1") or {}
    assert "res://hello" in user_resource_map
    assert "res://json/data" in user_resource_map
