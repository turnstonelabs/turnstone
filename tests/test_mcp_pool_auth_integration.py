"""Phase 6 integration tests — real-transport drives 401/403 through the SDK.

These are the structural exit criterion for Phase 6. They MUST drive
through the real ``streamablehttp_client``, the real httpx response-hook
path, and a REAL upstream MCP server (a ``FastMCP`` in-process subprocess
with a starlette middleware that programmatically returns 401/403 with
crafted ``WWW-Authenticate`` headers).

Direct ``httpx.HTTPStatusError`` injection is FORBIDDEN here — Phase 5
bug-1 was masked precisely by that pattern (the production code path
was structurally unreachable, but the unit-test injection bypassed the
SDK's swallow). The integration tests gate that the production path
actually receives the carrier signal end-to-end.

The fixture upstream is built in-thread (uvicorn on its own asyncio
loop in a background thread) — same pattern as
``tests/spike_sdk_concurrency.py``. Per the orchestrator's startup-cost
note, measured at ~0.05s per fixture spin-up locally; well under the
2s threshold for default-collection inclusion.
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

# Quiet noisy logs during tests.
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Fixture upstream — programmable BehaviorMiddleware
# ---------------------------------------------------------------------------


class BehaviorMiddleware(BaseHTTPMiddleware):
    """Inspects per-request behaviour state and returns 401/403 on demand.

    The behaviour is steered by a mutable ``behaviour`` dict on the
    middleware instance; tests mutate it via the fixture handle.
    Records every request's Authorization header for assertion.

    Behaviour semantics:
      * ``"once_401"``: return 401 once, then 200 thereafter.
      * ``"always_401"``: always return 401.
      * ``"once_403_insufficient"``: return 403 with insufficient_scope once.
      * ``"once_403_generic"``: return 403 without error param once.
      * ``"once_multi_www_auth_403"``: return 403 with TWO
        ``WWW-Authenticate`` headers — first ``Bearer`` challenge
        carries the SAFE scopes, second carries INJECTED scopes. The
        dispatcher must report only the first.
      * ``"never"`` (default): pass through to the real handler.

    ``www_authenticate`` overrides the default header crafted per shape.
    """

    def __init__(self, app: Any, behaviour: dict[str, Any]) -> None:
        super().__init__(app)
        self._behaviour = behaviour

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        from starlette.responses import Response as StarletteResponse

        # Record the Authorization header for assertion. POST is the
        # tools/call request the dispatcher sends.
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
                            'Bearer error="insufficient_scope", scope="files:write mail:send"',
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
        elif mode == "once_multi_www_auth_403" and not self._behaviour.get("_fired"):
            self._behaviour["_fired"] = True
            # Two ``WWW-Authenticate: Bearer ...`` challenges. The
            # first carries ``error=insufficient_scope`` but NO
            # ``scope=`` parameter; the second carries the INJECTED
            # scopes the dispatcher must NOT report. The first
            # challenge intentionally lacks ``scope`` because
            # ``parse_www_authenticate_bearer`` uses ``setdefault`` —
            # if the first challenge HAD a scope, ``setdefault`` would
            # already win on first-occurrence. The vector this test
            # guards is the case where a defended absence becomes a
            # silent presence: a hook regression to ``get(...)`` joins
            # repeated headers with ``, `` and the parser then folds
            # the second challenge's scope into the first challenge's
            # params dict because there is no first-occurrence to
            # protect.
            response = StarletteResponse("forbidden", status_code=403)
            response.headers.append(
                "www-authenticate",
                'Bearer realm="legit", error="insufficient_scope"',
            )
            response.headers.append(
                "www-authenticate",
                'Bearer error="insufficient_scope", scope="org:admin db:write"',
            )
            return response
        return await call_next(request)


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_server(port: int, behaviour: dict[str, Any]) -> uvicorn.Server:
    mcp = FastMCP(name="phase6-target", streamable_http_path="/mcp")

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
    """Boot a FastMCP fixture upstream in a background thread.

    Yields ``(url, behaviour)`` where ``behaviour`` is a mutable dict
    the test mutates to steer the middleware (set ``mode`` to one of
    the BehaviorMiddleware shapes).
    """
    port = _find_free_port()
    behaviour: dict[str, Any] = {}
    server = _build_server(port, behaviour)

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=_run, daemon=True, name="phase6-upstream")
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


# ---------------------------------------------------------------------------
# Test 21: 401 → refresh-and-retry → success
# ---------------------------------------------------------------------------


def test_integration_401_refresh_and_retry_succeeds(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Real upstream returns 401 once with ``WWW-Authenticate: Bearer
    error="invalid_token"``, then 200. Dispatcher carrier captures the
    401, ``force_refresh=True`` mints a new bearer (stubbed), retry
    succeeds. Hard invariant 3: breaker counter remains 0.

    Drives through the REAL ``streamablehttp_client`` and a REAL
    upstream subprocess (no ``httpx.HTTPStatusError`` injection). This
    is the structural exit gate for Phase 6 — the equivalent unit
    tests CANNOT prove the production wiring works because the SDK
    swallows the underlying exception.
    """
    url, behaviour = upstream
    behaviour["mode"] = "once_401"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    # Override URL to point at the local upstream (loopback http:// is
    # exempt from the URL-validator).
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="token", token="refreshed-bearer")
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ):
        result = mgr.call_tool_sync(
            "mcp__pool-srv__echo", {"payload": "hi"}, user_id="user-1", timeout=15
        )

    assert "echoed:hi" in result
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0
    # Server saw at least 2 POSTs to /mcp (initial + retry).
    post_headers = behaviour.get("post_auth_headers", [])
    assert len(post_headers) >= 2, f"expected >=2 POSTs; got {len(post_headers)}"
    # Retry carries a different bearer than the initial.
    initial = post_headers[0]
    retry = post_headers[1]
    assert initial != retry, (
        "retry attached the same bearer as the initial; the dispatcher "
        "did not pick up the refreshed token."
    )
    # Pool entry has a session after the successful retry.
    entry = mgr._user_pool_entries[("user-1", "pool-srv")]
    assert entry.session is not None


# ---------------------------------------------------------------------------
# Test 22: 401 + refresh failure → mcp_consent_required
# ---------------------------------------------------------------------------


def test_integration_401_with_refresh_failure_emits_consent_required(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, behaviour = upstream
    behaviour["mode"] = "once_401"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="refresh_failed")
        return TokenLookupResult(kind="token", token="access-aaa")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as exc_info,
    ):
        mgr.call_tool_sync("mcp__pool-srv__echo", {"payload": "x"}, user_id="user-1", timeout=15)

    # Structured-error envelopes flow back via ``RuntimeError(json_str)``
    # so the session-layer ``except Exception`` handler routes the
    # consent card uniformly across tool / resource / prompt dispatchers.
    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_consent_required"
    assert payload["error"]["server"] == "pool-srv"
    # Phase 8 — consent_url surfaces a /start URL the dashboard can open
    # in a popup. URL-encoded server name; no scopes baked in (the AS
    # picks up the configured scopes server-side at /start).
    assert payload["error"]["consent_url"] == "/v1/api/mcp/oauth/start?server=pool-srv"
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# Test 23: 403 + insufficient_scope → mcp_insufficient_scope with parsed scopes
# ---------------------------------------------------------------------------


def test_integration_403_insufficient_scope_emits_structured_error(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    url, behaviour = upstream
    behaviour["mode"] = "once_403_insufficient"
    behaviour["www_authenticate"] = (
        'Bearer error="insufficient_scope", scope="files:write mail:send"'
    )

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as exc_info,
    ):
        mgr.call_tool_sync("mcp__pool-srv__echo", {"payload": "x"}, user_id="user-1", timeout=15)

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_insufficient_scope"
    assert payload["error"]["scopes_required"] == ["files:write", "mail:send"]
    # Phase 8 — consent_url carries the step-up scopes URL-encoded so the
    # dashboard can union them with the configured set at /start.
    assert payload["error"]["consent_url"] == (
        "/v1/api/mcp/oauth/start?server=pool-srv&scopes=files%3Awrite%20mail%3Asend"
    )
    # No retry — exactly ONE POST attempted before the structured error.
    post_headers = behaviour.get("post_auth_headers", [])
    assert len(post_headers) == 1, (
        f"403 must NOT trigger a retry; observed {len(post_headers)} POSTs"
    )
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# Test 24: 403 without insufficient_scope → generic forbidden
# ---------------------------------------------------------------------------


def test_integration_403_no_insufficient_scope_emits_generic_forbidden(
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

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as exc_info,
    ):
        mgr.call_tool_sync("mcp__pool-srv__echo", {"payload": "x"}, user_id="user-1", timeout=15)

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_tool_call_forbidden"
    assert "scopes_required" not in payload["error"]
    post_headers = behaviour.get("post_auth_headers", [])
    assert len(post_headers) == 1, (
        f"403 must NOT trigger a retry; observed {len(post_headers)} POSTs"
    )


# ---------------------------------------------------------------------------
# sec-1: multi-WWW-Authenticate header injection — only the FIRST
# Bearer challenge feeds the structured-error / audit emission.
# ---------------------------------------------------------------------------


def test_integration_403_multi_www_authenticate_drops_injected_scopes(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Upstream returns a 403 with TWO ``WWW-Authenticate: Bearer ...``
    challenges. The first carries ``error=insufficient_scope`` but NO
    ``scope=`` parameter; the second carries INJECTED scopes
    (``["org:admin", "db:write"]``). The dispatcher must report
    ``scopes_required == []`` — derived from the first challenge alone
    — never the second challenge's injected scopes.

    Two layers of defence cooperate (either alone neutralises the
    vector; both run together so a regression in one cannot silently
    re-open it):

    1. ``_make_capturing_http_factory._hook`` reads
       ``response.headers.get_list("www-authenticate")[0]`` rather than
       ``response.headers.get(...)`` — the latter joins repeated
       headers with ``", "`` which the RFC 7235 tokenizer would
       otherwise consume as a continuation of the first challenge.
    2. ``parse_www_authenticate_bearer`` stops at the first ``Bearer``
       challenge boundary even if the input was already joined, so a
       hook regression to ``get(...)`` would NOT re-open the vector.

    The first challenge intentionally lacks ``scope=`` — the parser
    uses ``setdefault`` so a first-occurrence ``scope`` would already
    win and mask a single-layer regression. The undefended-absence
    case is what proves both layers actually do their job.

    Negative-test (CRITICAL — Phase 5 lesson): verified by reverting
    the hook to ``response.headers.get("www-authenticate")`` AND
    removing the ``_looks_like_bearer_challenge_start`` guard in
    ``parse_www_authenticate_bearer``. The test then fails because
    ``scopes_required`` becomes ``["org:admin", "db:write"]`` — the
    injected scopes from the second challenge silently fold into the
    first challenge's params dict via httpx's comma-joined header
    value (the absence of a first-occurrence scope means nothing
    blocks the fold).
    """
    url, behaviour = upstream
    behaviour["mode"] = "once_multi_www_auth_403"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="token", token="access-aaa")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as exc_info,
    ):
        mgr.call_tool_sync("mcp__pool-srv__echo", {"payload": "x"}, user_id="user-1", timeout=15)

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_insufficient_scope", (
        f"expected mcp_insufficient_scope; got {payload!r}"
    )
    # ``scopes_required`` derives from the FIRST challenge alone, which
    # carries no ``scope=`` parameter. The injected second challenge
    # MUST NOT appear here.
    assert payload["error"]["scopes_required"] == [], (
        "Multi-header injection slipped through: dispatcher reported "
        "scopes from the SECOND Bearer challenge. Got "
        f"{payload['error']['scopes_required']!r}; expected []."
    )


# ---------------------------------------------------------------------------
# Test 25: 401 retry ceiling — never recurse
# ---------------------------------------------------------------------------


def test_integration_401_retry_ceiling(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Upstream always returns 401; refresh stub keeps minting tokens.
    After exactly ONE retry, dispatcher emits ``mcp_consent_required``.
    """
    url, behaviour = upstream
    behaviour["mode"] = "always_401"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    refresh_count = 0

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        nonlocal refresh_count
        if kwargs.get("force_refresh"):
            refresh_count += 1
            return TokenLookupResult(kind="token", token=f"refreshed-{refresh_count}")
        return TokenLookupResult(kind="token", token="access-aaa")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ),
        pytest.raises(RuntimeError) as exc_info,
    ):
        mgr.call_tool_sync("mcp__pool-srv__echo", {"payload": "x"}, user_id="user-1", timeout=15)

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "mcp_consent_required"
    # Exactly ONE refresh round-trip.
    assert refresh_count == 1, f"expected exactly 1 refresh round-trip; got {refresh_count}"
    # Server saw EXACTLY 2 POSTs (initial + 1 retry).
    post_headers = behaviour.get("post_auth_headers", [])
    assert len(post_headers) == 2, (
        f"expected exactly 2 POSTs (initial + 1 retry); got {len(post_headers)}"
    )


# ---------------------------------------------------------------------------
# Test 26: breaker unaffected by repeated auth failures (slow — 50 cycles)
# ---------------------------------------------------------------------------


def test_integration_breaker_unaffected_by_auth_failures(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """50 sequential dispatches all hit 401 with refresh-failed → 50
    cycles of ``mcp_consent_required``. ``_consecutive_failures`` MUST
    stay at 0 throughout (hard invariant 3 verified end-to-end).
    """
    url, behaviour = upstream
    behaviour["mode"] = "always_401"

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="refresh_failed")
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ):
        for _ in range(50):
            with pytest.raises(RuntimeError) as exc_info:
                mgr.call_tool_sync(
                    "mcp__pool-srv__echo", {"payload": "x"}, user_id="user-1", timeout=15
                )
            payload = json.loads(str(exc_info.value))
            assert payload["error"]["code"] == "mcp_consent_required"
            assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# Test 27: static path unaffected by Phase 6 changes
# ---------------------------------------------------------------------------


def test_integration_static_path_unaffected(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Static-path connect against an unauthed upstream succeeds without
    going through the capturing factory. This is the integration-level
    mirror of ``test_reconnect_preserves_static_state_identity``.

    Drives the static path against the same fixture upstream (with
    ``behaviour={}`` so middleware passes through) — confirms the
    static path's session lifecycle is byte-identical even when the
    pool path's auth introspection is wired up.
    """
    url, _behaviour = upstream
    # No mode → middleware passes through to FastMCP.

    mgr, loop, _ = running_loop_mgr

    # Manually configure mgr with a static-path server pointing at the
    # fixture upstream. Use _connect_one (not the pool path).
    cfg = {"type": "streamable-http", "url": url}

    async def _connect_static() -> None:
        await mgr._connect_one("static-srv", cfg)

    fut = asyncio.run_coroutine_threadsafe(_connect_static(), loop)
    fut.result(timeout=15)

    state_before = mgr._static_servers.get("static-srv")
    assert state_before is not None
    assert state_before.session is not None
    # Snapshot identity.
    state_id_before = id(state_before)
    session_before = state_before.session

    # Reconnect — the canonical regression check is that the
    # StaticServerState object identity is preserved.
    fut = asyncio.run_coroutine_threadsafe(_connect_static(), loop)
    fut.result(timeout=15)

    state_after = mgr._static_servers.get("static-srv")
    assert state_after is not None
    assert id(state_after) == state_id_before, (
        "Static path StaticServerState identity changed across reconnect; "
        "hard invariant 1 violated."
    )
    assert state_after.session is not None
    assert state_after.session is not session_before, (
        "Reconnect did not actually replace the session"
    )


# ---------------------------------------------------------------------------
# Test 27b: static dispatch unaffected by Phase 8 consent_url kwarg
# ---------------------------------------------------------------------------


def test_static_dispatch_unaffected_by_consent_url_kwarg(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Static-auth tool dispatch must be byte-identical post-Phase 8.

    The Phase 8 changes only ADD a ``consent_url`` kwarg to
    ``_structured_error`` invocations on the pool path. Static dispatch
    must not pick up the field — there's no consent flow for
    ``auth_type='none'`` / ``'static'`` servers, and exposing one would
    confuse the dashboard renderer. Asserts a successful tool result is
    a plain string with no JSON envelope and no ``consent_url`` substring.
    """
    url, behaviour = upstream
    behaviour["mode"] = "never"  # passthrough — succeeds

    mgr, loop, _ = running_loop_mgr
    cfg = {"type": "streamable-http", "url": url}

    async def _connect_static() -> None:
        await mgr._connect_one("static-srv", cfg)

    fut = asyncio.run_coroutine_threadsafe(_connect_static(), loop)
    fut.result(timeout=15)

    # Drive call_tool_sync without a user_id — the static path is taken.
    result = mgr.call_tool_sync("mcp__static-srv__echo", {"payload": "static-x"}, timeout=15)

    # Static path returns the FastMCP fixture's echo string.
    assert "echoed:static-x" in result
    # No JSON envelope leaked through; specifically no consent_url field.
    assert "consent_url" not in result, (
        f"Static-auth tool dispatch surfaced a consent_url; result: {result!r}"
    )
    # Defensive: result is not a JSON-encoded structured error.
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        assert "error" not in parsed, (
            f"Static-auth dispatch returned a structured-error envelope; got {parsed!r}"
        )


# ---------------------------------------------------------------------------
# Test 28: pool reuse — 401 on a SECOND dispatch (carrier owned by entry)
# ---------------------------------------------------------------------------


def test_integration_pool_reuse_401_refresh_and_retry_succeeds(
    upstream: Any, running_loop_mgr: Any, storage: SQLiteBackend
) -> None:
    """Reused pool sessions still capture 401 correctly.

    Dispatch 1 hits a passthrough upstream (200) and populates
    ``entry.session``. Dispatch 2 reuses that session — no fresh
    connect, so a per-dispatch ``_AuthCapture`` would never reach
    the httpx response hook (the hook closes over the carrier passed
    at first connect, which lives on the entry). A correctly-wired
    entry-owned carrier is the only shape that lets dispatch 2's 401
    surface to the dispatcher.

    Two independent production bugs gate this test passing; both must
    hold for reused-session 401 recovery to work end-to-end:

    1. The carrier must live on the pool entry (not per-dispatch) so
       the response hook bound at first connect writes to the same
       object the dispatcher reads across reuse. Verified by reverting
       ``PoolEntryState.auth_capture`` to a per-dispatch
       ``_AuthCapture()`` allocation: the carrier-fired event never
       reaches the dispatcher and the test times out.

    2. The dispatcher must race ``call_tool`` against the carrier's
       fired event. The SDK's ``_receive_loop`` runs in BaseSession's
       TaskGroup nested inside ``streamablehttp_client``'s TaskGroup;
       when an upstream 4xx fires, the outer TaskGroup cancels
       ``_receive_loop`` mid-finally before it can deliver
       ``CONNECTION_CLOSED`` to the response stream's waiting
       receiver. anyio's ``send_nowait`` skips waiters with pending
       cancellation — but our dispatch task (created via
       ``run_coroutine_threadsafe`` for the reused-session case) has
       NO pending cancellation, so the send delivers but the receiver
       never wakes (the waiter's Event is set on stale state). Result:
       a forever-hung ``response_stream_reader.receive()``. Verified
       by reverting the ``asyncio.wait({call_task, fired_task})``
       race in ``_dispatch_pool_with_entry`` to a bare ``await
       session.call_tool(...)``: the test times out.

    This test is the structural gate against the per-dispatch carrier
    pattern: it looks right in code review and passes single-dispatch
    integration tests, but breaks silently on session reuse — and the
    SDK-level hang the carrier fix exposes silently strands the
    dispatcher even when the carrier is correct.
    """
    url, behaviour = upstream
    behaviour["mode"] = "never"  # passthrough — dispatch 1 succeeds

    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv", url=url)
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
        if kwargs.get("force_refresh"):
            return TokenLookupResult(kind="token", token="refreshed-bearer")
        return TokenLookupResult(kind="token", token="access-aaa")

    with patch(
        "turnstone.core.mcp_client.get_user_access_token_classified",
        side_effect=_fake_classified,
    ):
        # Dispatch 1: passthrough success. Establishes the pooled session.
        result1 = mgr.call_tool_sync(
            "mcp__pool-srv__echo", {"payload": "first"}, user_id="user-1", timeout=15
        )
        assert "echoed:first" in result1

        entry = mgr._user_pool_entries[("user-1", "pool-srv")]
        session_after_first = entry.session
        assert session_after_first is not None, (
            "test setup: dispatch 1 did not populate entry.session; "
            "subsequent dispatch will not exercise the reuse path"
        )

        # Reconfigure upstream to 401 once on the next call. Reset the
        # auth-headers log so we can count dispatch-2's POSTs cleanly.
        behaviour["post_auth_headers"] = []
        behaviour["mode"] = "once_401"
        behaviour["_fired"] = False

        # Dispatch 2: same (user, server). The hook from dispatch 1's
        # connect is still bound to entry.auth_capture. The 401 fires;
        # the dispatcher's auth_401 path triggers refresh-and-retry.
        result2 = mgr.call_tool_sync(
            "mcp__pool-srv__echo", {"payload": "second"}, user_id="user-1", timeout=15
        )

    assert "echoed:second" in result2, (
        f"reused-session 401 retry did not succeed. result: {result2!r}. "
        "If this is JSON with mcp_consent_required, the dispatcher "
        "fell through to consent_required emission; if a generic "
        "tool error, the carrier was empty (auth branch unreachable)."
    )
    assert mgr._consecutive_failures.get("pool-srv", 0) == 0, (
        "auth failures must not trip the per-server breaker"
    )

    # Dispatch 2 produces multiple POSTs: the original 401 with the
    # rejected bearer, then the retry's full connect handshake
    # (initialize + notifications/initialized + tools/list) followed by
    # the actual tools/call — all under the refreshed bearer. The retry
    # reconnects because the auth_401 handler evicted the broken session.
    post_headers = behaviour.get("post_auth_headers", [])
    assert len(post_headers) >= 2, (
        f"expected >=2 POSTs after dispatch 2 (401 + retry); "
        f"got {len(post_headers)}: {post_headers}"
    )
    # First POST is the original bearer that got 401'd.
    assert post_headers[0] == "Bearer access-aaa", (
        f"first POST was {post_headers[0]!r}; expected the original bearer"
    )
    # Every subsequent POST carries the refreshed bearer (the retry
    # ran with force_refresh=True and reconnected with the new token).
    refreshed = post_headers[1:]
    assert all(h == "Bearer refreshed-bearer" for h in refreshed), (
        f"retry POSTs carried unexpected bearer(s); observed: {post_headers}"
    )
