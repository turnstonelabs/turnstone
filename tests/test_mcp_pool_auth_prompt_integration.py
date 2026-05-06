"""Phase 7b integration tests — real-transport prompt get 401/403/etc.

Mirror of :mod:`tests.test_mcp_pool_auth_resource_integration` for the
prompt path (RFC §3.3). Drives through the real ``streamablehttp_client``,
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
    copy serves the prompt integration tests.
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
                            'Bearer error="insufficient_scope", scope="prompts:read"',
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
    mcp = FastMCP(name="phase7b-prompt-target", streamable_http_path="/mcp")

    @mcp.prompt()
    def greet(who: str = "world") -> str:
        return f"Hello, {who}!"

    @mcp.prompt()
    def summarize(topic: str = "today") -> str:
        return f"Please summarize {topic}."

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

    t = threading.Thread(target=_run, daemon=True, name="phase7b-prompt-upstream")
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


def _seed_pool_prompt_map(
    mgr: MCPClientManager,
    user_id: str,
    server_name: str,
    prefixed_name: str,
    original_name: str,
) -> None:
    """Pre-seed ``_user_prompt_map`` so ``_resolve_pool_target_prompt``
    finds the prefixed name. Production wires this through
    ``_connect_one_pool``; the integration tests seed it directly so the
    test focuses on the dispatch behaviour after resolution succeeds.
    """

    async def _seed() -> None:
        entry = await mgr._ensure_pool_entry((user_id, server_name))
        entry.prompts = [
            {
                "name": prefixed_name,
                "original_name": original_name,
                "server": server_name,
                "description": "",
                "arguments": [],
            }
        ]
        mgr._rebuild_user_prompt_map(user_id)

    assert mgr._loop is not None
    asyncio.run_coroutine_threadsafe(_seed(), mgr._loop).result(timeout=5)


# ---------------------------------------------------------------------------
# I-PR-1: 401 → refresh → retry → success (prompt path)
# ---------------------------------------------------------------------------


def test_prompt_get_401_refresh_and_retry_succeeds(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Real upstream returns 401 once, then 200. Carrier captures 401,
    force_refresh=True mints a new bearer, retry returns the prompt
    messages. Hard invariant 3: breaker counter remains 0.
    """
    url, behaviour = upstream
    behaviour["mode"] = "once_401"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_prompt_map(mgr, "user-1", "pool-srv", "mcp__pool-srv__greet", "greet")

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="token", token="refreshed-bearer")
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ):
        messages = mgr.get_prompt_sync(
            "mcp__pool-srv__greet",
            {"who": "everyone"},
            user_id="user-1",
            timeout=15,
        )

    assert isinstance(messages, list)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "everyone" in messages[0]["content"]
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
# I-PR-2: persistent 401 → mcp_consent_required (prompt path) → RuntimeError
# ---------------------------------------------------------------------------


def test_prompt_get_persistent_401_emits_consent_required(
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
    _seed_pool_prompt_map(mgr, "user-1", "pool-srv", "mcp__pool-srv__greet", "greet")

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="token", token="refreshed-bearer")
        return TokenLookupResult(kind="token", token="access-aaa")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as excinfo,
    ):
        mgr.get_prompt_sync(
            "mcp__pool-srv__greet",
            {"who": "world"},
            user_id="user-1",
            timeout=15,
        )

    payload = json.loads(str(excinfo.value))
    assert payload["error"]["code"] == "mcp_consent_required"
    assert payload["error"]["server"] == "pool-srv"
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# I-PR-3: 403 + insufficient_scope → mcp_insufficient_scope (prompt path)
# ---------------------------------------------------------------------------


def test_prompt_get_403_insufficient_scope_emits_structured_error(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, behaviour = upstream
    behaviour["mode"] = "once_403_insufficient"
    behaviour["www_authenticate"] = 'Bearer error="insufficient_scope", scope="prompts:read"'

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_prompt_map(mgr, "user-1", "pool-srv", "mcp__pool-srv__greet", "greet")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as excinfo,
    ):
        mgr.get_prompt_sync(
            "mcp__pool-srv__greet",
            {"who": "world"},
            user_id="user-1",
            timeout=15,
        )

    payload = json.loads(str(excinfo.value))
    assert payload["error"]["code"] == "mcp_insufficient_scope"
    assert payload["error"]["scopes_required"] == ["prompts:read"]
    post_headers = behaviour.get("post_auth_headers", [])
    assert len(post_headers) == 1, (
        f"403 must NOT trigger a retry; observed {len(post_headers)} POSTs"
    )
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# I-PR-3b: 403 generic → mcp_prompt_get_forbidden
# ---------------------------------------------------------------------------


def test_prompt_get_403_generic_forbidden(
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
    _seed_pool_prompt_map(mgr, "user-1", "pool-srv", "mcp__pool-srv__greet", "greet")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as excinfo,
    ):
        mgr.get_prompt_sync(
            "mcp__pool-srv__greet",
            {"who": "world"},
            user_id="user-1",
            timeout=15,
        )

    payload = json.loads(str(excinfo.value))
    # Per the kind="prompt" wiring of `_handle_auth_403`, the
    # operation-specific code surfaces here rather than the tool path's
    # generic mcp_tool_call_forbidden.
    assert payload["error"]["code"] == "mcp_prompt_get_forbidden"
    assert "scopes_required" not in payload["error"]


# ---------------------------------------------------------------------------
# I-PR-6: breaker isolation — auth failures NEVER trip the breaker
# ---------------------------------------------------------------------------


def test_prompt_get_breaker_unaffected_by_auth_failures(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Repeated 401 + refresh-failed cycles leave breaker at 0
    (hard invariant 3 verified end-to-end for the prompt path)."""
    url, behaviour = upstream
    behaviour["mode"] = "always_401"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_prompt_map(mgr, "user-1", "pool-srv", "mcp__pool-srv__greet", "greet")

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="refresh_failed")
        return TokenLookupResult(kind="token", token="access-aaa")

    # Re-seed each iteration: symmetric eviction (Phase 7b) clears
    # ``_user_prompt_map`` on auth failure so the next dispatch's
    # resolver would miss without a fresh seed. Production reconnect
    # repopulates this; the test simulates that out-of-band.
    for _ in range(10):
        _seed_pool_prompt_map(mgr, "user-1", "pool-srv", "mcp__pool-srv__greet", "greet")
        with (
            patch(
                "turnstone.core.mcp_client.get_user_access_token_classified",
                side_effect=_fake_classified,
            ),
            pytest.raises(RuntimeError) as excinfo,
        ):
            mgr.get_prompt_sync(
                "mcp__pool-srv__greet",
                {"who": "world"},
                user_id="user-1",
                timeout=15,
            )
        payload = json.loads(str(excinfo.value))
        assert payload["error"]["code"] == "mcp_consent_required"
        assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# Negative tests — token lookup edge cases (prompt path)
# ---------------------------------------------------------------------------


def test_prompt_get_missing_token_emits_consent_required(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, _behaviour = upstream
    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_prompt_map(mgr, "user-1", "pool-srv", "mcp__pool-srv__greet", "greet")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="missing")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as excinfo,
    ):
        mgr.get_prompt_sync(
            "mcp__pool-srv__greet",
            {"who": "world"},
            user_id="user-1",
            timeout=10,
        )

    payload = json.loads(str(excinfo.value))
    assert payload["error"]["code"] == "mcp_consent_required"


def test_prompt_get_decrypt_failure_emits_token_undecryptable(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, _behaviour = upstream
    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    _seed_pool_prompt_map(mgr, "user-1", "pool-srv", "mcp__pool-srv__greet", "greet")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="decrypt_failure")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as excinfo,
    ):
        mgr.get_prompt_sync(
            "mcp__pool-srv__greet",
            {"who": "world"},
            user_id="user-1",
            timeout=10,
        )

    payload = json.loads(str(excinfo.value))
    assert payload["error"]["code"] == "mcp_token_undecryptable_key_unknown"


def test_prompt_get_http_url_emits_url_insecure(
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
    _seed_pool_prompt_map(mgr, "user-1", "pool-srv", "mcp__pool-srv__greet", "greet")

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as excinfo,
    ):
        mgr.get_prompt_sync(
            "mcp__pool-srv__greet",
            {"who": "world"},
            user_id="user-1",
            timeout=5,
        )

    payload = json.loads(str(excinfo.value))
    assert payload["error"]["code"] == "mcp_oauth_url_insecure"


def test_prompt_get_unknown_name_raises_value_error(
    running_loop_mgr: Any,
) -> None:
    """When the prefixed name doesn't resolve to either pool or static,
    the static-path code raises ``ValueError``. Per-user-first
    resolution (scope decision 0.1) means user_id-bearing callers still
    hit this path when their pool catalog doesn't carry the name."""
    mgr, _loop, _ = running_loop_mgr
    with pytest.raises(ValueError, match="Unknown MCP prompt"):
        mgr.get_prompt_sync(
            "mcp__nonexistent__missing",
            None,
            user_id="user-1",
            timeout=5,
        )


# ---------------------------------------------------------------------------
# I-PR-E2E: real discovery + dispatch in same connect (no _seed_pool_prompt_map)
# ---------------------------------------------------------------------------


def test_prompt_get_e2e_discovery_then_dispatch_succeeds(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Drive REAL discovery + dispatch end-to-end through the pool path.

    Mirror of the tool path's
    ``test_integration_pool_reuse_401_refresh_and_retry_succeeds``: skips
    the ``_seed_pool_prompt_map`` shortcut and lets ``_connect_one_pool``
    populate ``_user_prompt_map`` from the real ``prompts/list``
    upstream response. Verifies that the entry's discovered prompts
    match what the FastMCP fixture advertises AND that
    ``_user_prompt_map[user_id]`` is populated with the prefixed name
    after dispatch — proving the discovery path actually fired.

    This is the structural gate against a regression where prompt
    dispatch silently bypasses discovery (e.g., a mis-wired resolver
    that finds the (server, original) via prefix-parsing alone never
    populates the per-user catalog).
    """
    url, behaviour = upstream
    behaviour["mode"] = "never"  # passthrough — discovery + dispatch both succeed

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))
    # NB: no `_seed_pool_prompt_map` — the resolver finds (server, original)
    # via the `mcp__{server}__{prompt}` prefix and hands off to
    # ``_dispatch_pool_prompt_sync``, which lazy-connects via
    # ``_connect_one_pool``. The connect runs the real ``prompts/list``
    # against the FastMCP fixture and populates the per-user catalog.

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ):
        messages = mgr.get_prompt_sync(
            "mcp__pool-srv__greet",
            {"who": "world"},
            user_id="user-1",
            timeout=15,
        )

    assert isinstance(messages, list)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "world" in messages[0]["content"]
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0

    # Discovery populated the entry's prompts with both fixtures
    # (``greet`` and ``summarize``) — proves real ``prompts/list``
    # ran during the connect, not just the targeted ``prompts/get``.
    entry = mgr._user_pool_entries[("user-1", "pool-srv")]
    assert entry.session is not None
    assert entry.prompts is not None
    discovered_names = {p["name"] for p in entry.prompts}
    assert "mcp__pool-srv__greet" in discovered_names
    assert "mcp__pool-srv__summarize" in discovered_names

    # ``_rebuild_user_prompt_map`` ran during the connect, populating the
    # per-user catalog. This is the signal that discovery wired into the
    # routing tables — without it, a follow-up ``get_prompt_sync`` would
    # need to re-resolve via prefix parsing every time.
    user_prompt_map = mgr._user_prompt_map.get("user-1") or {}
    assert "mcp__pool-srv__greet" in user_prompt_map
    assert "mcp__pool-srv__summarize" in user_prompt_map
