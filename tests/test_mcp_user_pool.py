"""Tests for the per-(user, server) MCP session pool.

Covers Phase 5 of the OAuth-MCP rollout: pool data structures,
``_ensure_pool_entry`` lazy allocation, ``_connect_one_pool`` plumbing,
the dispatch state machine in ``_dispatch_pool``, idle / LRU eviction,
failure classification, and ``user_id`` thread-through.

The static path (``auth_type ∈ {none, static}``) MUST stay
byte-identical — see ``test_mcp_client.py``'s
``test_reconnect_preserves_static_state_identity``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import make_mcp_token_cipher
from turnstone.core.mcp_client import MCPClientManager, PoolEntryState
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    """A fresh SQLite backend per test (not the shared singleton)."""
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
) -> None:
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    store = MCPTokenStore(storage, cipher, node_id="test")
    store.create_user_token(
        user_id,
        server_name,
        access_token=access_token,
        refresh_token="refresh-rrr",
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
    """Background-loop fixture matching the static-path test convention.

    Tests that need a wired-up app_state assign it via ``mgr.set_app_state``.
    """
    cfg: dict[str, Any] = {}
    mgr = MCPClientManager(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="mcp-pool-test-loop")
    thread.start()
    mgr._loop = loop
    try:
        yield mgr, loop, thread
    finally:
        # Drain the eviction task before stopping the loop so its log/stream
        # handlers don't fire after pytest has torn its handlers down. Mirrors
        # the production ``shutdown()`` shape.
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


def _run_on_loop(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """Submit *coro* to *loop*, wait for the result with a 5s timeout."""
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=5)


# ---------------------------------------------------------------------------
# Pool data structures
# ---------------------------------------------------------------------------


class TestPoolDataStructures:
    """``_user_pool_entries``, ``_user_pool_locks``, eviction-task state."""

    def test_pool_state_starts_empty(self) -> None:
        mgr = MCPClientManager({})
        assert mgr._user_pool_entries == {}
        assert mgr._user_pool_last_used == {}
        assert mgr._user_pool_locks == {}
        assert mgr._user_pool_eviction_task is None

    def test_set_app_state_persists(self) -> None:
        mgr = MCPClientManager({})
        sentinel = SimpleNamespace(token_store=object())
        mgr.set_app_state(sentinel)
        assert mgr._app_state is sentinel

    def test_ensure_pool_entry_allocates_lock_on_loop(self, running_loop_mgr) -> None:
        """``asyncio.Lock`` MUST be created on the mcp-loop (RFC §2.0 #2)."""
        mgr, loop, _thread = running_loop_mgr
        key = ("user-A", "pool-srv")
        entry = _run_on_loop(loop, mgr._ensure_pool_entry(key))
        assert isinstance(entry, PoolEntryState)
        assert entry.key == key
        assert isinstance(entry.open_lock, asyncio.Lock)
        # Calling again returns the same entry / lock object.
        entry2 = _run_on_loop(loop, mgr._ensure_pool_entry(key))
        assert entry2 is entry
        assert entry2.open_lock is entry.open_lock


# ---------------------------------------------------------------------------
# Lazy connect (`_connect_one_pool`)
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Awaitable async context manager that returns ``value`` from __aenter__."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class TestLazyConnect:
    def test_connect_pool_injects_authorization_header(self, running_loop_mgr) -> None:
        from unittest.mock import patch

        mgr, loop, _ = running_loop_mgr

        observed_kwargs: dict[str, Any] = {}

        async def _probe(*_args: Any, **_kwargs: Any) -> None:
            return None

        fake_session = MagicMock()
        fake_session.initialize = AsyncMock(return_value=None)

        def _stream_factory(*, url: str, headers: dict[str, str]) -> _AsyncCM:
            observed_kwargs["url"] = url
            observed_kwargs["headers"] = dict(headers)
            return _AsyncCM((AsyncMock(), AsyncMock(), lambda: None))

        with (
            patch("turnstone.core.mcp_client.streamablehttp_client", side_effect=_stream_factory),
            patch.object(mgr, "_tcp_probe", side_effect=_probe),
            patch("turnstone.core.mcp_client.ClientSession", return_value=_AsyncCM(fake_session)),
        ):
            cfg = {
                "type": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "headers": {},
            }
            entry = _run_on_loop(
                loop,
                mgr._connect_one_pool(("user-1", "pool-srv"), cfg, "access-aaa"),
            )

        assert entry.session is fake_session
        assert observed_kwargs["headers"]["Authorization"] == "Bearer access-aaa"

    def test_connect_pool_rejects_non_http_transport(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        cfg = {"type": "stdio", "command": "echo"}
        with pytest.raises(RuntimeError, match="streamable-http"):
            _run_on_loop(
                loop,
                mgr._connect_one_pool(("user-1", "pool-srv"), cfg, "access-aaa"),
            )

    def test_pool_path_does_not_touch_static_servers(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        # Pre-seed a static-path entry so accidental writes are observable.
        from turnstone.core.mcp_client import StaticServerState

        sentinel = StaticServerState(name="static-srv", session=MagicMock())
        mgr._static_servers["static-srv"] = sentinel

        async def _seed_pool() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            entry.session = MagicMock()
            entry.last_used = time.monotonic()

        _run_on_loop(loop, _seed_pool())
        # Pool side has its own state; the static dict is untouched.
        assert mgr._static_servers["static-srv"] is sentinel
        assert mgr._user_pool_entries[("user-1", "pool-srv")].session is not None


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


class TestEviction:
    def test_idle_eviction_closes_stale_entries(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 0.0  # everything is stale

        async def _seed() -> list[PoolEntryState]:
            entries = []
            for i in range(3):
                entry = await mgr._ensure_pool_entry((f"u{i}", "pool-srv"))
                entry.session = MagicMock()
                entries.append(entry)
            return entries

        _run_on_loop(loop, _seed())

        async def _evict() -> None:
            await mgr._evict_idle_pool_entries()

        _run_on_loop(loop, _evict())
        assert mgr._user_pool_entries == {}

    def test_eviction_skips_locked_entries(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 0.0

        async def _seed_and_lock() -> tuple[asyncio.Lock, asyncio.Event]:
            entry = await mgr._ensure_pool_entry(("u-busy", "pool-srv"))
            entry.session = MagicMock()
            held = asyncio.Event()

            async def _hold() -> None:
                async with entry.open_lock:
                    held.set()
                    await asyncio.sleep(0.5)

            asyncio.create_task(_hold())
            await held.wait()
            return entry.open_lock, held

        _run_on_loop(loop, _seed_and_lock())

        async def _evict() -> None:
            await mgr._evict_idle_pool_entries()

        _run_on_loop(loop, _evict())
        # Entry survives because eviction skipped the locked key.
        assert ("u-busy", "pool-srv") in mgr._user_pool_entries

    def test_lru_cap_evicts_oldest(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 999_999.0  # TTL effectively disabled
        mgr._user_pool_lru_max = 2

        async def _seed() -> None:
            base = time.monotonic()
            for i in range(5):
                key = (f"u{i}", "pool-srv")
                entry = await mgr._ensure_pool_entry(key)
                entry.session = MagicMock()
                # Recent timestamps so TTL doesn't fire — only LRU should.
                entry.last_used = base + i
                mgr._user_pool_last_used[key] = base + i

        _run_on_loop(loop, _seed())

        async def _evict() -> None:
            await mgr._evict_idle_pool_entries()

        _run_on_loop(loop, _evict())
        assert len(mgr._user_pool_entries) <= 2
        # The two newest survive (u3, u4).
        assert ("u4", "pool-srv") in mgr._user_pool_entries
        assert ("u3", "pool-srv") in mgr._user_pool_entries

    def test_eviction_resilient_to_close_errors(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 0.0

        broken_stack = MagicMock(spec=AsyncExitStack)
        broken_stack.aclose = AsyncMock(side_effect=RuntimeError("close failed"))

        async def _seed() -> None:
            for i in range(2):
                entry = await mgr._ensure_pool_entry((f"u{i}", "pool-srv"))
                entry.session = MagicMock()
                entry.stack = broken_stack

        _run_on_loop(loop, _seed())

        async def _evict() -> None:
            await mgr._evict_idle_pool_entries()

        # Eviction must not raise even if close fails.
        _run_on_loop(loop, _evict())
        # All entries removed from the dict regardless.
        assert mgr._user_pool_entries == {}


# ---------------------------------------------------------------------------
# Dispatch state machine
# ---------------------------------------------------------------------------


class TestDispatchStateMachine:
    """One row per state in the §1.5 / RFC §6 state machine."""

    def _wire_pool(
        self, mgr: MCPClientManager, storage: SQLiteBackend, cipher: Any
    ) -> SimpleNamespace:
        mgr.set_storage(storage)
        state = _make_app_state(storage, cipher=cipher)
        mgr.set_app_state(state)
        return state

    def test_no_token_emits_consent_required(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        mgr, _loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        self._wire_pool(mgr, storage, cipher)

        result = mgr.call_tool_sync(
            "mcp__pool-srv__do_thing",
            {},
            user_id="user-1",
            timeout=5,
        )
        payload = json.loads(result)
        assert payload["error"]["code"] == "mcp_consent_required"
        assert payload["error"]["server"] == "pool-srv"

    def test_decrypt_failure_does_not_emit_consent(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        from turnstone.core.mcp_crypto import MCPTokenDecryptError

        mgr, _loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        state = self._wire_pool(mgr, storage, cipher)

        def _raise(*args, **kwargs):
            raise MCPTokenDecryptError(
                "no installed key can decrypt",
                key_fingerprints_attempted=("aabbccdd",),
            )

        state.mcp_token_store.get_user_token = _raise

        result = mgr.call_tool_sync(
            "mcp__pool-srv__do_thing",
            {},
            user_id="user-1",
            timeout=5,
        )
        payload = json.loads(result)
        assert payload["error"]["code"] == "mcp_token_undecryptable_key_unknown"
        # Operator fingerprints stay server-side (audit log + structured log);
        # the agent-facing payload must NOT carry them onward to the LLM
        # provider.
        assert "key_fingerprints_attempted" not in payload["error"]

    def test_refresh_failure_emits_consent(self, running_loop_mgr, storage: SQLiteBackend) -> None:
        mgr, _loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        # Seed an expired token with no refresh — the classified getter
        # treats this as "refresh_failed" (deletes the row, returns the
        # tagged result).
        _seed_user_token(storage, cipher, expires_in_seconds=-1000)
        state = self._wire_pool(mgr, storage, cipher)
        # Drop the refresh token to force the no-refresh-token branch.
        state.mcp_token_store.delete_user_token("user-1", "pool-srv")
        state.mcp_token_store.create_user_token(
            "user-1",
            "pool-srv",
            access_token="access-aaa",
            refresh_token=None,
            expires_at=(datetime.now(UTC) - timedelta(seconds=1000)).strftime("%Y-%m-%dT%H:%M:%S"),
            scopes="openid",
            as_issuer="https://as.example.com",
            audience="https://mcp.example.com",
        )

        result = mgr.call_tool_sync(
            "mcp__pool-srv__do_thing",
            {},
            user_id="user-1",
            timeout=5,
        )
        payload = json.loads(result)
        assert payload["error"]["code"] == "mcp_consent_required"

    def test_token_present_dispatches_to_session(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher, expires_in_seconds=3600)
        self._wire_pool(mgr, storage, cipher)

        # Pre-seed a connected pool entry so dispatch never touches the
        # SDK or the network.
        fake_session = MagicMock()

        async def _call_tool(name, args):
            content = MagicMock()
            content.text = "tool-result"
            res = MagicMock()
            res.content = [content]
            res.isError = False
            return res

        fake_session.call_tool = _call_tool

        async def _seed_entry() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            entry.session = fake_session

        _run_on_loop(loop, _seed_entry())

        result = mgr.call_tool_sync(
            "mcp__pool-srv__do_thing",
            {"q": "hi"},
            user_id="user-1",
            timeout=5,
        )
        assert result == "tool-result"


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class TestClassifyFailure:
    def test_transport_failure_classified_as_transport(self) -> None:
        mgr = MCPClientManager({})
        for exc in (
            BrokenPipeError(),
            ConnectionResetError(),
            EOFError(),
            TimeoutError("net"),
        ):
            assert mgr._classify_failure(exc) == "transport"

    def test_protocol_error_classified_as_protocol(self) -> None:
        from mcp import McpError
        from mcp.types import ErrorData

        mgr = MCPClientManager({})
        err = McpError(ErrorData(code=-32600, message="bad request"))
        assert mgr._classify_failure(err) == "protocol"

    def test_other_classified_as_other(self) -> None:
        mgr = MCPClientManager({})
        assert mgr._classify_failure(ValueError("nope")) == "other"

    def test_http_401_classified_as_auth(self) -> None:
        import httpx

        mgr = MCPClientManager({})
        req = httpx.Request("POST", "https://mcp.example.com/sse")
        resp = httpx.Response(401, request=req)
        exc = httpx.HTTPStatusError("unauthorized", request=req, response=resp)
        assert mgr._classify_failure(exc) == "auth"

    def test_http_403_classified_as_auth(self) -> None:
        import httpx

        mgr = MCPClientManager({})
        req = httpx.Request("POST", "https://mcp.example.com/sse")
        resp = httpx.Response(403, request=req)
        exc = httpx.HTTPStatusError("forbidden", request=req, response=resp)
        assert mgr._classify_failure(exc) == "auth"

    def test_http_500_not_classified_as_auth(self) -> None:
        import httpx

        mgr = MCPClientManager({})
        req = httpx.Request("POST", "https://mcp.example.com/sse")
        resp = httpx.Response(500, request=req)
        exc = httpx.HTTPStatusError("server", request=req, response=resp)
        # 5xx is not auth — falls through to "other".
        assert mgr._classify_failure(exc) == "other"


# ---------------------------------------------------------------------------
# Wired-failure paths in _dispatch_pool
# ---------------------------------------------------------------------------


class TestDispatchFailureWiring:
    """``_classify_failure`` is consulted in production, not just tests."""

    def _wire_pool(
        self, mgr: MCPClientManager, storage: SQLiteBackend, cipher: Any
    ) -> SimpleNamespace:
        mgr.set_storage(storage)
        state = _make_app_state(storage, cipher=cipher)
        mgr.set_app_state(state)
        return state

    def _seed_connected_session(
        self, mgr: MCPClientManager, loop: asyncio.AbstractEventLoop, exc: BaseException
    ) -> None:
        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            sess = MagicMock()

            async def _raise(*_args: Any, **_kwargs: Any) -> Any:
                raise exc

            sess.call_tool = _raise
            entry.session = sess

        _run_on_loop(loop, _seed())

    def test_dispatch_pool_401_does_not_trip_breaker(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        import httpx

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        self._wire_pool(mgr, storage, cipher)

        req = httpx.Request("POST", "https://mcp.example.com/sse")
        resp = httpx.Response(401, request=req)
        self._seed_connected_session(
            mgr, loop, httpx.HTTPStatusError("unauthorized", request=req, response=resp)
        )

        with pytest.raises(httpx.HTTPStatusError):
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )
        # Auth failure must NOT tick the per-server breaker.
        assert mgr._consecutive_failures.get("pool-srv", 0) == 0
        # But the pool entry's session must be cleared so the next call
        # re-authenticates with a fresh bearer.
        entry = mgr._user_pool_entries[("user-1", "pool-srv")]
        assert entry.session is None

    def test_dispatch_pool_transport_failure_trips_breaker(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        self._wire_pool(mgr, storage, cipher)

        self._seed_connected_session(mgr, loop, BrokenPipeError("dead"))

        with pytest.raises(BrokenPipeError):
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )
        # Transport failure ticks the breaker.
        assert mgr._consecutive_failures.get("pool-srv", 0) == 1


# ---------------------------------------------------------------------------
# HTTPS enforcement (sec-1)
# ---------------------------------------------------------------------------


class TestHttpsEnforcement:
    def test_pool_rejects_http_url_for_oauth_user(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        mgr, _loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv", url="http://insecure.example.com/sse")
        _seed_user_token(storage, cipher)
        mgr.set_storage(storage)
        state = _make_app_state(storage, cipher=cipher)
        mgr.set_app_state(state)

        result = mgr.call_tool_sync(
            "mcp__pool-srv__do_thing",
            {},
            user_id="user-1",
            timeout=5,
        )
        payload = json.loads(result)
        assert payload["error"]["code"] == "mcp_oauth_url_insecure"
        assert payload["error"]["server"] == "pool-srv"

    def test_pool_accepts_loopback_http(self, running_loop_mgr, storage: SQLiteBackend) -> None:
        """``http://127.0.0.1`` and ``http://localhost`` should not be blocked."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv", url="http://127.0.0.1:8000/sse")
        _seed_user_token(storage, cipher)
        mgr.set_storage(storage)
        state = _make_app_state(storage, cipher=cipher)
        mgr.set_app_state(state)

        # Pre-seed a connected pool entry so dispatch succeeds without
        # touching the network.
        fake_session = MagicMock()

        async def _call_tool(name, args):
            content = MagicMock()
            content.text = "ok"
            res = MagicMock()
            res.content = [content]
            res.isError = False
            return res

        fake_session.call_tool = _call_tool

        async def _seed_entry() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            entry.session = fake_session

        _run_on_loop(loop, _seed_entry())

        result = mgr.call_tool_sync(
            "mcp__pool-srv__do_thing",
            {},
            user_id="user-1",
            timeout=5,
        )
        # Loopback URL not rejected — dispatch reaches the (fake) session.
        assert result == "ok"

    def test_validate_oauth_user_url_helper(self) -> None:
        from turnstone.core.mcp_client import _validate_oauth_user_url

        # Acceptable: https + the exact loopback hostnames.
        _validate_oauth_user_url("https://mcp.example.com/sse")
        _validate_oauth_user_url("http://localhost/sse")
        _validate_oauth_user_url("http://127.0.0.1:9000/sse")
        _validate_oauth_user_url("http://[::1]/sse")

        # Rejected: non-https + non-loopback. The ``*.localhost`` suffix
        # bypass is intentionally NOT honored (RFC 6761 localhost-zone
        # resolution is configuration-dependent — custom resolvers,
        # /etc/hosts, Docker overlays may map ``foo.localhost`` to
        # non-loopback IPs).
        for bad in (
            "http://mcp.example.com/sse",
            "http://app.localhost/sse",
            "ws://mcp.example.com/sse",
            "ftp://mcp.example.com/sse",
            "//mcp.example.com/sse",
        ):
            with pytest.raises(ValueError, match="https://"):
                _validate_oauth_user_url(bad)


# ---------------------------------------------------------------------------
# _resolve_pool_target parser (q-9)
# ---------------------------------------------------------------------------


class TestResolvePoolTarget:
    def _make_mgr_with_oauth_server(
        self, storage: SQLiteBackend, *, name: str = "pool-srv"
    ) -> MCPClientManager:
        _seed_oauth_server(storage, name=name)
        mgr = MCPClientManager({})
        mgr.set_storage(storage)
        return mgr

    def test_malformed_prefix(self, storage: SQLiteBackend) -> None:
        mgr = self._make_mgr_with_oauth_server(storage)
        # Wrong prefix.
        assert mgr._resolve_pool_target("xyz__pool-srv__t", None, None) is None

    def test_too_few_separators(self, storage: SQLiteBackend) -> None:
        mgr = self._make_mgr_with_oauth_server(storage)
        # mcp__server with no original_name segment.
        assert mgr._resolve_pool_target("mcp__pool-srv", None, None) is None

    def test_empty_server_segment(self, storage: SQLiteBackend) -> None:
        mgr = self._make_mgr_with_oauth_server(storage)
        # mcp____tool — server segment is empty.
        assert mgr._resolve_pool_target("mcp____tool", None, None) is None

    def test_original_with_double_underscore_round_trips(self, storage: SQLiteBackend) -> None:
        mgr = self._make_mgr_with_oauth_server(storage)
        target = mgr._resolve_pool_target("mcp__pool-srv__do__thing", None, None)
        assert target is not None
        assert target[0] == "pool-srv"
        # Original-name keeps its embedded ``__``.
        assert target[1] == "do__thing"


# ---------------------------------------------------------------------------
# LRU + lock interlock (q-7)
# ---------------------------------------------------------------------------


class TestLruInterlock:
    def test_lru_cap_skips_locked_oldest(self, running_loop_mgr) -> None:
        """LRU eviction must skip a locked entry the same way TTL does."""
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 999_999.0  # disable TTL
        mgr._user_pool_lru_max = 2

        async def _seed_and_lock_oldest() -> tuple[asyncio.Lock, asyncio.Event]:
            base = time.monotonic()
            for i in range(3):
                key = (f"u{i}", "pool-srv")
                entry = await mgr._ensure_pool_entry(key)
                entry.session = MagicMock()
                # Older index ⇒ older timestamp.
                entry.last_used = base + i
                mgr._user_pool_last_used[key] = base + i
            # Lock the oldest (u0) so eviction must skip it and pick a younger one.
            oldest = mgr._user_pool_entries[("u0", "pool-srv")]
            held = asyncio.Event()

            async def _hold() -> None:
                async with oldest.open_lock:
                    held.set()
                    await asyncio.sleep(0.5)

            asyncio.create_task(_hold())
            await held.wait()
            return oldest.open_lock, held

        _run_on_loop(loop, _seed_and_lock_oldest())

        async def _evict() -> None:
            await mgr._evict_idle_pool_entries()

        _run_on_loop(loop, _evict())
        # Locked u0 must survive.
        assert ("u0", "pool-srv") in mgr._user_pool_entries
        # The oldest unlocked entry (u1) was evicted to bring count down to cap.
        assert ("u1", "pool-srv") not in mgr._user_pool_entries


# ---------------------------------------------------------------------------
# Concurrent dispatch on shared session (M4 / perf-1)
# ---------------------------------------------------------------------------


class TestConcurrentDispatch:
    def test_pool_concurrent_dispatch_to_same_user_server_is_concurrent(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """Two tool calls on the SAME (user, server) must overlap in flight,
        not serialize on ``open_lock``."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        mgr.set_storage(storage)
        state = _make_app_state(storage, cipher=cipher)
        mgr.set_app_state(state)

        observed_max_concurrency = 0
        in_flight = 0
        in_flight_lock = threading.Lock()

        async def _call_tool(name, args):
            nonlocal observed_max_concurrency, in_flight
            with in_flight_lock:
                in_flight += 1
                observed_max_concurrency = max(observed_max_concurrency, in_flight)
            try:
                # Hold a moment so concurrent calls overlap.
                await asyncio.sleep(0.1)
                content = MagicMock()
                content.text = "ok"
                res = MagicMock()
                res.content = [content]
                res.isError = False
                return res
            finally:
                with in_flight_lock:
                    in_flight -= 1

        fake_session = MagicMock()
        fake_session.call_tool = _call_tool

        async def _seed_entry() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            entry.session = fake_session

        _run_on_loop(loop, _seed_entry())

        results: list[str] = []
        errors: list[Exception] = []

        def _dispatch() -> None:
            try:
                results.append(
                    mgr.call_tool_sync(
                        "mcp__pool-srv__do_thing",
                        {},
                        user_id="user-1",
                        timeout=5,
                    )
                )
            except Exception as exc:  # pragma: no cover — diagnostic only
                errors.append(exc)

        t1 = threading.Thread(target=_dispatch)
        t2 = threading.Thread(target=_dispatch)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert errors == []
        assert results == ["ok", "ok"]
        # Both dispatches were in flight at the same time — open_lock did
        # not serialize them.
        assert observed_max_concurrency == 2


# ---------------------------------------------------------------------------
# user_id thread-through (signature)
# ---------------------------------------------------------------------------


class TestUserIdThreadThrough:
    def test_default_user_id_takes_static_path(self, running_loop_mgr) -> None:
        """``user_id=None`` must leave the static-path call byte-identical."""
        mgr, _loop, _ = running_loop_mgr
        # Static-path tool registered the standard way.
        mgr._tool_map["mcp__static__t"] = ("static-srv", "t")
        from turnstone.core.mcp_client import StaticServerState

        fake_session = MagicMock()

        async def _call_tool(name, args):
            content = MagicMock()
            content.text = "static-output"
            res = MagicMock()
            res.content = [content]
            res.isError = False
            return res

        fake_session.call_tool = _call_tool
        mgr._static_servers["static-srv"] = StaticServerState(
            name="static-srv", session=fake_session
        )

        # No user_id, no app_state — pool branch is skipped entirely.
        result = mgr.call_tool_sync("mcp__static__t", {"q": "hi"}, user_id=None, timeout=5)
        assert result == "static-output"

    def test_user_id_with_static_path_does_not_use_pool(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """Caller passes user_id but the resolved server is static — pool
        branch must not run because ``_lookup_server_row`` reports
        ``auth_type != 'oauth_user'``."""
        mgr, _loop, _ = running_loop_mgr
        storage.create_mcp_server(
            server_id="srv-static",
            name="static-srv",
            transport="stdio",
            url="",
            command="echo",
            auth_type="static",
        )
        mgr.set_storage(storage)
        mgr.set_app_state(SimpleNamespace())

        mgr._tool_map["mcp__static-srv__t"] = ("static-srv", "t")
        from turnstone.core.mcp_client import StaticServerState

        fake_session = MagicMock()

        async def _call_tool(name, args):
            content = MagicMock()
            content.text = "static-output"
            res = MagicMock()
            res.content = [content]
            res.isError = False
            return res

        fake_session.call_tool = _call_tool
        mgr._static_servers["static-srv"] = StaticServerState(
            name="static-srv", session=fake_session
        )

        result = mgr.call_tool_sync(
            "mcp__static-srv__t",
            {"q": "hi"},
            user_id="user-1",
            timeout=5,
        )
        assert result == "static-output"
        # No pool entries were created.
        assert mgr._user_pool_entries == {}
