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
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_mcp_token_cipher, stop_loop_thread
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
            # ``_static_health_task`` included: since the BaseExceptionGroup
            # hardening, ``_connect_all`` reliably starts (and keeps alive) the
            # health loop even when every configured connect fails — a test
            # that drives ``_connect_all`` must drain it like production
            # ``shutdown()`` does, or the task is destroyed pending at GC.
            for attr in (
                "_user_pool_eviction_task",
                "_user_token_sweep_task",
                "_static_health_task",
            ):
                task = getattr(m, attr)
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    setattr(m, attr, None)
            # Close any parked pool transport owners a successful
            # ``_connect_one_pool`` left installed, mirroring production
            # ``shutdown()`` — an undrained owner is destroyed pending at GC.
            for entry in list(m._user_pool_entries.values()):
                owner = entry.owner_task
                if owner is not None and not owner.done():
                    if entry.close_requested is not None:
                        entry.close_requested.set()
                    owner.cancel()
                    await asyncio.gather(owner, return_exceptions=True)

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_drain(mgr), loop).result(timeout=2)
        stop_loop_thread(loop, thread)


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
        # Phase 7b: ``_connect_one_pool`` discovers tools, resources,
        # and prompts after ``initialize()`` returns (resources/prompts
        # capability-gated). The capability stub returns a tools-only
        # advertisement so the test can keep its narrow focus on the
        # bearer-injection contract; resources/prompts paths are
        # exercised by the real-transport tests in
        # ``tests/test_mcp_user_catalog.py``.
        fake_caps = MagicMock()
        fake_caps.resources = None
        fake_caps.prompts = None
        fake_session.get_server_capabilities = MagicMock(return_value=fake_caps)
        fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

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

    def test_eviction_resilient_to_owner_unwind_errors(self, running_loop_mgr) -> None:
        """Owner-model successor to the old ``resilient_to_close_errors`` test.

        Teardown reaps the entry's owner through a bounded ``asyncio.wait`` that
        never re-raises, so even an owner whose in-task unwind raises cannot
        break eviction. The old failure mode this guarded — a cross-task
        ``stack.aclose()`` raising ``RuntimeError('...different task...')`` — is
        structurally impossible now: the transport cms live in, and unwind in,
        the owner task, never the evictor.
        """
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 0.0

        async def _seed() -> None:
            for i in range(2):
                key = (f"u{i}", "pool-srv")
                entry = await mgr._ensure_pool_entry(key)
                event = asyncio.Event()

                async def _owner(ev: asyncio.Event = event) -> None:
                    await ev.wait()
                    raise RuntimeError("unwind failed")

                owner = asyncio.create_task(_owner(), name=f"mcp-pool-owner-test:{i}")
                # Retrieve the exception so the raising owner doesn't warn at GC.
                owner.add_done_callback(lambda t: None if t.cancelled() else t.exception())
                entry.session = MagicMock()
                entry.owner_task = owner
                entry.close_requested = event

        _run_on_loop(loop, _seed())

        async def _evict() -> None:
            await mgr._evict_idle_pool_entries()

        # Eviction must not raise even if the owner's unwind raises.
        _run_on_loop(loop, _evict())
        # All entries removed from the dict regardless.
        assert mgr._user_pool_entries == {}

    @staticmethod
    def _fake_tools(server_name: str, i: int) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": f"mcp__{server_name}__t{i}",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def test_idle_eviction_cools_entries_for_live_listener_users(self, running_loop_mgr) -> None:
        """TTL eviction cools (retains) a live-listener user's
        catalog-bearing entry and full-drops a listener-less user's —
        the #836 split. A second tick must not disturb the cooled entry
        (the already-cooled skip)."""
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 0.0  # everything is stale

        async def _seed() -> None:
            for i in range(2):
                entry = await mgr._ensure_pool_entry((f"u{i}", "pool-srv"))
                entry.session = MagicMock()
                entry.tools = self._fake_tools("pool-srv", i)

        _run_on_loop(loop, _seed())
        # The server must exist in the pool registries for retention.
        mgr._oauth_user_server_names = {"pool-srv"}
        # u0 has a live session (tool listener); u1 does not.
        mgr.add_listener(lambda: None, user_id="u0")

        _run_on_loop(loop, mgr._evict_idle_pool_entries())

        # u0: cooled — retained without a session, catalog intact, and
        # the dead bearer copy cleared with the transport.
        cooled = mgr._user_pool_entries.get(("u0", "pool-srv"))
        assert cooled is not None
        assert cooled.session is None
        assert cooled.tools is not None
        assert cooled.bound_token is None
        # u1: full drop.
        assert ("u1", "pool-srv") not in mgr._user_pool_entries

        # Second tick: the cooled entry is skipped, not re-processed.
        _run_on_loop(loop, mgr._evict_idle_pool_entries())
        assert ("u0", "pool-srv") in mgr._user_pool_entries

    def test_eviction_drops_cooled_entry_when_server_leaves_registry(
        self, running_loop_mgr
    ) -> None:
        """Registry-liveness: a cooled entry is retained ONLY while its
        server still exists as a pool server. Admin delete / disable /
        rename / flip-to-static all remove the name from the pool
        registries at reconcile — the ghost catalog must leave live
        sessions within one tick, not survive for the session's life
        (pre-#836-fix the TTL bounded this to ~10 minutes)."""
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 0.0

        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("u0", "pool-srv"))
            entry.session = MagicMock()
            entry.tools = self._fake_tools("pool-srv", 0)

        _run_on_loop(loop, _seed())
        mgr._oauth_user_server_names = {"pool-srv"}
        mgr.add_listener(lambda: None, user_id="u0")

        # While registered: cooled + retained.
        _run_on_loop(loop, mgr._evict_idle_pool_entries())
        assert ("u0", "pool-srv") in mgr._user_pool_entries

        # Auth-flip between POOL types keeps retention (still pool-backed;
        # the reconcile flip self-heal re-primes the real catalog).
        mgr._oauth_user_server_names = set()
        mgr._obo_server_names = {"pool-srv"}
        _run_on_loop(loop, mgr._evict_idle_pool_entries())
        assert ("u0", "pool-srv") in mgr._user_pool_entries

        # Server leaves the pool registries entirely (deleted / disabled /
        # renamed / flipped to static): full drop despite the live listener.
        mgr._obo_server_names = set()
        _run_on_loop(loop, mgr._evict_idle_pool_entries())
        assert ("u0", "pool-srv") not in mgr._user_pool_entries
        assert "u0" not in mgr._user_tool_map

    def test_drop_catalog_locked_serializes_with_inflight_connect(self, running_loop_mgr) -> None:
        """A revocation drop must wait for an in-flight connect holding
        ``open_lock`` — an unserialized drop is republished (resurrected)
        by the connect's discovery, with nothing left to ever clear it."""
        mgr, loop, _ = running_loop_mgr
        mgr._oauth_user_server_names = {"pool-srv"}
        key = ("u0", "pool-srv")

        async def _scenario() -> tuple[bool, bool]:
            entry = await mgr._ensure_pool_entry(key)
            entry.session = MagicMock()
            entry.tools = self._fake_tools("pool-srv", 0)
            mgr._rebuild_user_tool_map("u0")
            # Simulate an in-flight connect: open_lock held while the
            # "discovery" publishes the catalog.
            await entry.open_lock.acquire()
            drop_task = asyncio.create_task(mgr._drop_catalog_locked(key))
            await asyncio.sleep(0)
            # Drop is parked on the lock — catalog still published.
            blocked = entry.tools is not None and not drop_task.done()
            # Connect finishes its publish, then releases the lock.
            mgr._rebuild_user_tool_map("u0")
            entry.open_lock.release()
            await drop_task
            cleared = (
                entry.tools is None and entry.session is None and "u0" not in mgr._user_tool_map
            )
            return blocked, cleared

        blocked, cleared = _run_on_loop(loop, _scenario())
        assert blocked, "drop must wait for the in-flight connect's open_lock"
        assert cleared, "drop must win once the connect completes — no resurrection"

    def test_lookup_grant_dead_requires_wired_infrastructure(self, running_loop_mgr) -> None:
        """kind='missing' is authoritative only when the stores that
        could know are wired: the obo lookup returns 'missing' for an
        unconfigured token store / storage too, and a boot-window blip
        must not clear a user's catalog (it can't re-prime until a new
        session). With the stores present, missing / refresh_failed /
        the empty-token fallback classify as dead; transient and
        decrypt failures never do."""
        mgr, _loop, _ = running_loop_mgr

        missing = SimpleNamespace(kind="missing", token=None)
        # No app_state at all → not authoritative.
        assert mgr._lookup_grant_dead(missing) is False
        # Token store present but storage unwired → not authoritative.
        mgr._app_state = SimpleNamespace(mcp_token_store=object())
        mgr._storage = None
        assert mgr._lookup_grant_dead(missing) is False
        # Fully wired → authoritative.
        mgr._storage = object()
        assert mgr._lookup_grant_dead(missing) is True
        assert mgr._lookup_grant_dead(SimpleNamespace(kind="refresh_failed", token=None)) is True
        assert mgr._lookup_grant_dead(SimpleNamespace(kind="token", token="")) is True
        assert mgr._lookup_grant_dead(SimpleNamespace(kind="token", token="tok")) is False
        assert (
            mgr._lookup_grant_dead(SimpleNamespace(kind="refresh_failed_transient", token=None))
            is False
        )
        assert mgr._lookup_grant_dead(SimpleNamespace(kind="decrypt_failure", token=None)) is False
        # Token store unconfigured on a wired app_state → not authoritative.
        mgr._app_state = SimpleNamespace(mcp_token_store=None)
        assert mgr._lookup_grant_dead(missing) is False

    def test_status_reports_cooled_catalog_as_idle(self, running_loop_mgr) -> None:
        """A cooled entry's catalog is still model-visible, so status
        must not report '0 tools' for it: counts fall back to the
        cooled catalog, ``connected`` stays transport-truthful, and the
        idle pool is surfaced separately."""
        mgr, loop, _ = running_loop_mgr
        mgr._oauth_user_server_names = {"pool-srv"}

        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("u0", "pool-srv"))
            entry.tools = self._fake_tools("pool-srv", 0)

        _run_on_loop(loop, _seed())

        status = mgr._oauth_user_server_status("pool-srv", "u0")
        assert status["connected"] is False
        assert status["tools"] == 1
        assert status["user_pools"] == 0
        assert status["user_pools_idle"] == 1
        # Another user sees nothing (per-user scoping unchanged).
        other = mgr._oauth_user_server_status("pool-srv", "u-other")
        assert other["tools"] == 0
        assert other["user_pools_idle"] == 0
        # Aggregate operator view counts the idle catalog too.
        agg = mgr._oauth_user_server_status("pool-srv", None, aggregate=True)
        assert agg["tools"] == 1
        assert agg["user_pools_idle"] == 1

    def test_idle_eviction_drops_catalogless_stub_despite_live_listener(
        self, running_loop_mgr
    ) -> None:
        """A cold, never-discovered stub (``_ensure_pool_entry``
        allocated, connect failed before discovery) carries no catalog
        worth retaining — TTL eviction drops it even for a live-listener
        user, so revoke-cleared and connect-failed entries can't
        accumulate as zombies behind an open session."""
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 0.0

        async def _seed() -> None:
            await mgr._ensure_pool_entry(("u-stub", "pool-srv"))

        _run_on_loop(loop, _seed())
        mgr._oauth_user_server_names = {"pool-srv"}
        mgr.add_listener(lambda: None, user_id="u-stub")

        _run_on_loop(loop, mgr._evict_idle_pool_entries())
        assert ("u-stub", "pool-srv") not in mgr._user_pool_entries

    def test_lru_cap_ignores_cooled_entries(self, running_loop_mgr) -> None:
        """The LRU cap bounds WARM entries (connection resources), not
        cooled catalog-only ones — cooled entries neither count toward
        the cap nor get evicted by it."""
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 999_999.0  # TTL effectively disabled
        mgr._user_pool_lru_max = 2

        async def _seed() -> None:
            base = time.monotonic()
            # Three cooled entries (no session/owner, catalog present).
            for i in range(3):
                key = (f"cool{i}", "pool-srv")
                entry = await mgr._ensure_pool_entry(key)
                entry.tools = self._fake_tools("pool-srv", i)
                entry.last_used = base + i
                mgr._user_pool_last_used[key] = base + i
            # Two warm entries, newer than the cooled ones.
            for i in range(2):
                key = (f"warm{i}", "pool-srv")
                entry = await mgr._ensure_pool_entry(key)
                entry.session = MagicMock()
                entry.tools = self._fake_tools("pool-srv", 10 + i)
                entry.last_used = base + 10 + i
                mgr._user_pool_last_used[key] = base + 10 + i

        _run_on_loop(loop, _seed())
        mgr._oauth_user_server_names = {"pool-srv"}
        for i in range(3):
            mgr.add_listener(lambda: None, user_id=f"cool{i}")

        _run_on_loop(loop, mgr._evict_idle_pool_entries())

        # Warm count (2) is at the cap — nothing evicted, cooled
        # entries (which would be "oldest" by last_used) untouched.
        assert len(mgr._user_pool_entries) == 5
        assert all((f"cool{i}", "pool-srv") in mgr._user_pool_entries for i in range(3))

    def test_lru_cap_cools_live_listener_entries(self, running_loop_mgr) -> None:
        """Over the cap, warm entries of live-listener users are COOLED
        (transport closed, entry + catalog retained) oldest-first until
        the warm count meets the cap — cap pressure must not reintroduce
        the #836 tool loss for live sessions."""
        mgr, loop, _ = running_loop_mgr
        mgr._user_pool_idle_ttl_s = 999_999.0
        mgr._user_pool_lru_max = 1

        async def _seed() -> None:
            base = time.monotonic()
            for i in range(3):
                key = (f"u{i}", "pool-srv")
                entry = await mgr._ensure_pool_entry(key)
                entry.session = MagicMock()
                entry.tools = self._fake_tools("pool-srv", i)
                entry.last_used = base + i
                mgr._user_pool_last_used[key] = base + i

        _run_on_loop(loop, _seed())
        mgr._oauth_user_server_names = {"pool-srv"}
        for i in range(3):
            mgr.add_listener(lambda: None, user_id=f"u{i}")

        _run_on_loop(loop, mgr._evict_idle_pool_entries())

        # All three entries survive with catalogs; only the newest is
        # still warm.
        assert len(mgr._user_pool_entries) == 3
        warm = [
            key
            for key, e in mgr._user_pool_entries.items()
            if e.session is not None or e.owner_task is not None
        ]
        assert warm == [("u2", "pool-srv")]
        for i in range(3):
            assert mgr._user_pool_entries[(f"u{i}", "pool-srv")].tools is not None


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

    def _seed_cooled_catalog(self, mgr: MCPClientManager, loop) -> list[int]:
        """Seed a cooled catalog-bearing entry + live listener for user-1.

        Returns the listener's fire counter. Used by the dead-grant rows:
        a dispatch that learns the grant is GONE must drop this catalog so
        live sessions converge with the revocation (#836 cross-node
        disconnect) instead of re-offering the revoked tools.
        """

        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            entry.tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "mcp__pool-srv__do_thing",
                        "description": "",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
            mgr._rebuild_user_tool_map("user-1")

        _run_on_loop(loop, _seed())
        mgr._oauth_user_server_names = {"pool-srv"}
        fired = [0]

        def _cb() -> None:
            fired[0] += 1

        mgr.add_listener(_cb, user_id="user-1")
        assert mgr.is_mcp_tool("mcp__pool-srv__do_thing", user_id="user-1") is True
        return fired

    def test_no_token_emits_consent_required(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        self._wire_pool(mgr, storage, cipher)
        fired = self._seed_cooled_catalog(mgr, loop)

        with pytest.raises(RuntimeError) as exc_info:
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_consent_required"
        assert payload["error"]["server"] == "pool-srv"
        # kind="missing" is a DEAD grant: the retained catalog drops and
        # the live session is notified — tools leave instead of dangling
        # behind a consent card for access the user no longer holds.
        # The drop is SCHEDULED (never awaited on the dispatch path, which
        # may be parked behind a long same-key call) — flush the loop.
        _run_on_loop(loop, asyncio.sleep(0.05))
        assert mgr._user_pool_entries[("user-1", "pool-srv")].tools is None
        assert mgr.is_mcp_tool("mcp__pool-srv__do_thing", user_id="user-1") is False
        assert fired[0] == 1

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

        with pytest.raises(RuntimeError) as exc_info:
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_token_undecryptable_key_unknown"
        # Operator fingerprints stay server-side (audit log + structured log);
        # the agent-facing payload must NOT carry them onward to the LLM
        # provider.
        assert "key_fingerprints_attempted" not in payload["error"]

    def test_refresh_failure_emits_consent(self, running_loop_mgr, storage: SQLiteBackend) -> None:
        mgr, loop, _ = running_loop_mgr
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
        fired = self._seed_cooled_catalog(mgr, loop)

        with pytest.raises(RuntimeError) as exc_info:
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_consent_required"
        # kind="refresh_failed" is likewise a DEAD grant → catalog drops
        # (scheduled — flush the loop before asserting).
        _run_on_loop(loop, asyncio.sleep(0.05))
        assert mgr._user_pool_entries[("user-1", "pool-srv")].tools is None
        assert fired[0] == 1

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

    def test_http_401_classified_as_auth_401(self) -> None:
        """Defense-in-depth: ``HTTPStatusError`` classification still works
        even though Phase 6 normally consults the carrier instead.

        Phase 6 split ``"auth"`` into ``"auth_401"`` / ``"auth_403"``
        so the dispatcher can refresh-and-retry only on 401.
        """
        import httpx

        mgr = MCPClientManager({})
        req = httpx.Request("POST", "https://mcp.example.com/sse")
        resp = httpx.Response(401, request=req)
        exc = httpx.HTTPStatusError("unauthorized", request=req, response=resp)
        assert mgr._classify_failure(exc) == "auth_401"

    def test_http_403_classified_as_auth_403(self) -> None:
        import httpx

        mgr = MCPClientManager({})
        req = httpx.Request("POST", "https://mcp.example.com/sse")
        resp = httpx.Response(403, request=req)
        exc = httpx.HTTPStatusError("forbidden", request=req, response=resp)
        assert mgr._classify_failure(exc) == "auth_403"

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

        with pytest.raises(RuntimeError) as exc_info:
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )
        payload = json.loads(str(exc_info.value))
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
    def test_pool_concurrent_dispatch_to_same_user_server_is_serialized(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """Phase 6: two tool calls on the SAME (user, server) MUST serialize
        on ``open_lock`` so the auth-introspection carrier never crosses
        between concurrent dispatches.

        Phase 5 perf-1 released ``open_lock`` before ``call_tool`` so two
        concurrent same-key calls multiplexed on a shared
        ``ClientSession``. Phase 6 reverts that for the auth-aware path
        because the per-dispatch ``_AuthCapture`` is keyed off the
        ``httpx.AsyncClient`` event hook — releasing the lock would let
        a concurrent dispatch overwrite the carrier mid-flight,
        attributing one caller's 401 to another (a security bug).

        Verified by reverting ``_dispatch_pool_with_entry`` to the
        Phase 5 shape (release ``open_lock`` before ``call_tool`` —
        i.e. move the ``in_flight += 1`` / ``call_tool`` / decrement
        block out of the ``async with`` body) and confirming this test
        observes ``max_concurrency == 2``.
        """
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
                # Hold a moment so concurrent calls would overlap if
                # they weren't serialized on ``open_lock``.
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
        # ``open_lock`` held across ``call_tool`` — the second dispatch
        # waits for the first to release before entering call_tool.
        assert observed_max_concurrency == 1


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


class TestOboPriming:
    """oauth_obo servers must be primed at session start — it is the ONLY path
    that warms their pool + surfaces their tools (no consent flow exists), so a
    regression here makes the whole feature inert (review finding, mcp_client.py
    :2597)."""

    def test_prime_routes_obo_server_through_mint_and_warms_pool(
        self, running_loop_mgr, storage
    ) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        mgr.set_storage(storage)
        app_state = _make_app_state(storage, cipher=cipher)
        # The priming pre-check reads oidc_config.issuer and requires a stored
        # credential row (existence only, no decrypt) before running any
        # per-server obo work.
        app_state.oidc_config = SimpleNamespace(issuer="https://idp.example.com")
        mgr.set_app_state(app_state)
        storage.upsert_oidc_user_credential(
            "user-1", "https://idp.example.com", refresh_token_ct=b"ct"
        )
        storage.create_mcp_server(
            server_id="srv-obo",
            name="obo-srv",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_obo",
            oauth_audience="api://mcp-a",
        )
        mgr._obo_server_names = {"obo-srv"}
        mgr._oauth_user_server_names = set()

        warmed: list[tuple[Any, Any, str]] = []

        async def _fake_prime_server(key: Any, cfg: Any, token: str) -> None:
            warmed.append((key, cfg, token))

        obo_lookup = AsyncMock(return_value=SimpleNamespace(kind="token", token="minted-at"))
        user_lookup = AsyncMock()
        with (
            patch.object(mgr, "_prime_user_server", new=_fake_prime_server),
            patch("turnstone.core.mcp_client.get_obo_access_token_classified", new=obo_lookup),
            patch("turnstone.core.mcp_client.get_user_access_token_classified", new=user_lookup),
        ):
            _run_on_loop(loop, mgr._prime_user_pools("user-1"))

        # The obo server was minted (not routed to the oauth_user path) and warmed.
        obo_lookup.assert_awaited_once()
        assert obo_lookup.await_args.kwargs["server_name"] == "obo-srv"
        user_lookup.assert_not_awaited()
        assert len(warmed) == 1
        assert warmed[0][2] == "minted-at"

    def test_prime_skips_obo_server_when_no_credential(self, running_loop_mgr, storage) -> None:
        """A user without a captured credential is skipped BEFORE any
        per-server obo work: one existence SELECT decides all obo servers
        (credential-less users previously paid three SQL reads per obo server
        per session start just to learn kind='missing'). The pool is not
        warmed, the mint machinery never runs, and nothing raises — the
        re-login rail handles it on real dispatch."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        mgr.set_storage(storage)
        app_state = _make_app_state(storage, cipher=cipher)
        app_state.oidc_config = SimpleNamespace(issuer="https://idp.example.com")
        mgr.set_app_state(app_state)
        storage.create_mcp_server(
            server_id="srv-obo",
            name="obo-srv",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_obo",
            oauth_audience="api://mcp-a",
        )
        mgr._obo_server_names = {"obo-srv"}

        warmed: list[Any] = []

        async def _fake_prime_server(key: Any, cfg: Any, token: str) -> None:
            warmed.append(key)

        obo_lookup = AsyncMock(return_value=SimpleNamespace(kind="missing", token=None))
        with (
            patch.object(mgr, "_prime_user_server", new=_fake_prime_server),
            patch("turnstone.core.mcp_client.get_obo_access_token_classified", new=obo_lookup),
        ):
            _run_on_loop(loop, mgr._prime_user_pools("user-1"))

        obo_lookup.assert_not_awaited()
        assert warmed == []


class TestPendingConsentClearGate:
    """The dispatch-success pending-consent clear must (a) NOT issue SQL on
    every call (no new hot-path SQL) yet (b) still clear cross-node. The
    ``_pending_consent_cleared`` TTL map dedupes the DELETE per (user, server)
    per TTL window — a permanent cleared-set would suppress the clear forever
    on a node that cleared before ANOTHER node wrote a fresh badge."""

    def test_first_success_clears_then_dedupes(self) -> None:
        mgr = MCPClientManager({})
        mgr._storage = MagicMock()
        # First success on a fresh pair → one DELETE (self-heals a badge that may
        # have been written on ANOTHER node — no "we wrote it" precondition).
        mgr._clear_pending_consent_sync("u1", "srv")
        mgr._storage.delete_mcp_pending_consent.assert_called_once_with("u1", "srv")
        # Subsequent successes within the TTL skip the SQL.
        mgr._storage.delete_mcp_pending_consent.reset_mock()
        mgr._clear_pending_consent_sync("u1", "srv")
        mgr._clear_pending_consent_sync("u1", "srv")
        mgr._storage.delete_mcp_pending_consent.assert_not_called()

    def test_a_new_failure_write_re_arms_the_clear(self) -> None:
        mgr = MCPClientManager({})
        mgr._storage = MagicMock()
        mgr._clear_pending_consent_sync("u1", "srv")  # clears + marks cleared
        mgr._storage.delete_mcp_pending_consent.reset_mock()
        # A fresh pending-consent write un-marks the pair...
        mgr._write_pending_consent(
            "u1", "srv", error_code="mcp_consent_required", scopes_required=None
        )
        # ...so the next success clears again without waiting out the TTL.
        mgr._clear_pending_consent_sync("u1", "srv")
        mgr._storage.delete_mcp_pending_consent.assert_called_once_with("u1", "srv")

    def test_ttl_expiry_re_clears_cross_node_badges(self) -> None:
        """The cross-node self-heal (review finding): node A cleared the pair,
        then node B wrote a fresh badge — node A has no local signal, so its
        cleared entry must AGE OUT and the next success re-run the DELETE.
        With a permanent set the badge survived until node A restarted."""
        from turnstone.core.mcp_client import _PENDING_CONSENT_CLEAR_TTL_SECONDS

        mgr = MCPClientManager({})
        mgr._storage = MagicMock()
        mgr._clear_pending_consent_sync("u1", "srv")
        mgr._storage.delete_mcp_pending_consent.reset_mock()
        # Age the entry past the TTL (simulates time passing on node A while
        # node B writes a badge this node never observes).
        mgr._pending_consent_cleared[("u1", "srv")] -= _PENDING_CONSENT_CLEAR_TTL_SECONDS + 1
        mgr._clear_pending_consent_sync("u1", "srv")
        mgr._storage.delete_mcp_pending_consent.assert_called_once_with("u1", "srv")

    def test_cleared_map_prunes_at_size_threshold(self) -> None:
        """The TTL map is bounded: crossing the size threshold drops expired
        entries (and worst-case the oldest half), so a long-lived node serving
        many (user, server) pairs can't grow it without bound. Pruning is
        memory hygiene only — a pruned pair just re-runs one DELETE later."""
        from unittest.mock import patch as _patch

        mgr = MCPClientManager({})
        mgr._storage = MagicMock()
        with _patch("turnstone.core.mcp_client._PENDING_CONSENT_CLEARED_MAX", 4):
            for i in range(4):
                mgr._clear_pending_consent_sync("u", f"srv-{i}")
            assert len(mgr._pending_consent_cleared) == 4
            # Age everything out; the next insert prunes the expired entries.
            from turnstone.core.mcp_client import _PENDING_CONSENT_CLEAR_TTL_SECONDS

            for key in list(mgr._pending_consent_cleared):
                mgr._pending_consent_cleared[key] -= _PENDING_CONSENT_CLEAR_TTL_SECONDS + 1
            mgr._clear_pending_consent_sync("u", "srv-new")
            assert ("u", "srv-new") in mgr._pending_consent_cleared
            assert len(mgr._pending_consent_cleared) == 1


class TestTokenRejectedDetail:
    def test_obo_rows_are_not_pointed_at_a_consent_flow(self) -> None:
        """Review finding: the 401-retry-exhausted branches told oauth_obo
        users 'Re-consent required' with consent_url=None — a dead end, since
        no per-server consent flow exists for sign-in passthrough. The obo
        detail points at the admin (audience/config) instead."""
        from turnstone.core.mcp_client import _token_rejected_detail

        user_detail = _token_rejected_detail({"auth_type": "oauth_user"})
        assert "Re-consent required" in user_detail

        obo_detail = _token_rejected_detail({"auth_type": "oauth_obo"})
        assert "consent" not in obo_detail.lower()
        assert "administrator" in obo_detail.lower()

    def test_obo_insufficient_scope_detail_points_at_admin_not_reconsent(self) -> None:
        """Review finding: the 403 insufficient-scope branch was the one
        user-actionable site not made obo-aware — it told obo users to
        'Re-consent with new scopes' though no per-server consent flow exists
        (consent_url is None, /start rejects obo). The obo detail names the real
        remedy (an administrator widening access), the oauth_user one keeps the
        step-up re-consent language."""
        from turnstone.core.mcp_client import _pool_error_detail

        user_detail = _pool_error_detail(
            {"auth_type": "oauth_user"}, "insufficient_scope", kind="tool"
        )
        assert "Re-consent" in user_detail

        obo_detail = _pool_error_detail(
            {"auth_type": "oauth_obo"}, "insufficient_scope", kind="tool"
        )
        assert "consent" not in obo_detail.lower()
        assert "administrator" in obo_detail.lower()


# ---------------------------------------------------------------------------
# Background token-freshness sweep (oauth_user keep-hot, no connection warming)
# ---------------------------------------------------------------------------


class TestUserTokenFreshnessSweep:
    """The background sweep that keeps every consented ``oauth_user`` grant hot
    for unattended / autonomous work: refresh-on-expiry via the canonical path,
    proactive dead-grant badging, once-only surfacing, and — the load-bearing
    property — total invisibility to static / no-auth deployments."""

    def _wire(self, mgr: MCPClientManager, storage: SQLiteBackend, cipher: Any) -> None:
        mgr.set_storage(storage)
        mgr.set_app_state(_make_app_state(storage, cipher=cipher))
        mgr._oauth_user_server_names = {"pool-srv"}

    @staticmethod
    def _classified(kind: str, token: str | None = None):
        async def _fake(**kwargs: Any) -> Any:
            return SimpleNamespace(kind=kind, token=token)

        return _fake

    # -- no-auth / static safety: the sweep must be structurally invisible ----

    def test_sweep_noop_without_oauth_servers(self, running_loop_mgr, storage) -> None:
        """A static-only / no-auth deployment: the OBO gate returns before any
        DB scan or AS round-trip — the single most important property."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        mgr._oauth_user_server_names = set()  # no oauth_user server configured
        storage.list_mcp_user_token_reconcile_targets = MagicMock(return_value=[])  # type: ignore[method-assign]

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=AsyncMock(),
        ) as classified:
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        storage.list_mcp_user_token_reconcile_targets.assert_not_called()  # no token-table scan
        classified.assert_not_awaited()  # no AS round-trip

    def test_sweep_noop_before_storage_wired(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._oauth_user_server_names = {"pool-srv"}  # oauth configured but app not wired yet
        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=AsyncMock(),
        ) as classified:
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        classified.assert_not_awaited()

    def test_sweep_skips_server_not_in_oauth_set(self, running_loop_mgr, storage) -> None:
        """A token row lingering for a since-demoted / renamed server is not
        reconciled — only pairs whose server is currently ``oauth_user``."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        _seed_user_token(storage, cipher, user_id="u1", server_name="ghost-srv")

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=AsyncMock(),
        ) as classified:
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        classified.assert_not_awaited()  # ghost-srv is not in _oauth_user_server_names

    # -- classification branches --------------------------------------------

    def test_healthy_token_no_badge(self, running_loop_mgr, storage) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        _seed_user_token(storage, cipher, user_id="u1", server_name="pool-srv")
        storage.upsert_mcp_pending_consent = MagicMock()  # type: ignore[method-assign]

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=self._classified("token", token="access-aaa"),
        ):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        storage.upsert_mcp_pending_consent.assert_not_called()
        assert ("u1", "pool-srv") not in mgr._token_sweep_warned

    def test_dead_grant_badges_once_and_dedups(self, running_loop_mgr, storage, caplog) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        _seed_user_token(storage, cipher, user_id="u1", server_name="pool-srv")
        storage.upsert_mcp_pending_consent = MagicMock()  # type: ignore[method-assign]

        with (
            patch(
                "turnstone.core.mcp_client.get_user_access_token_classified",
                new=self._classified("refresh_failed"),
            ),
            caplog.at_level(logging.WARNING, logger="turnstone.core.mcp_client"),
        ):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
            _run_on_loop(loop, mgr._sweep_user_token_freshness())  # second tick: no re-badge

        # Badge raised exactly once, proactively, with the dashboard's code.
        storage.upsert_mcp_pending_consent.assert_called_once()
        assert (
            storage.upsert_mcp_pending_consent.call_args.kwargs["error_code"]
            == "mcp_consent_required"
        )
        assert ("u1", "pool-srv") in mgr._token_sweep_warned
        escalations = [r for r in caplog.records if "needs re-consent" in r.getMessage()]
        assert len(escalations) == 1  # logged loud-once, not every tick

    def test_decrypt_failure_warns_but_does_not_badge(self, running_loop_mgr, storage) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        _seed_user_token(storage, cipher, user_id="u1", server_name="pool-srv")
        storage.upsert_mcp_pending_consent = MagicMock()  # type: ignore[method-assign]

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=self._classified("decrypt_failure"),
        ):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        # Operator-actionable (key unknown) — surfaced in the warned set, but NOT
        # a user-consent badge (outside the dashboard's scope).
        storage.upsert_mcp_pending_consent.assert_not_called()
        assert ("u1", "pool-srv") in mgr._token_sweep_warned

    def test_transient_failure_is_silent(self, running_loop_mgr, storage) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        _seed_user_token(storage, cipher, user_id="u1", server_name="pool-srv")
        storage.upsert_mcp_pending_consent = MagicMock()  # type: ignore[method-assign]

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=self._classified("refresh_failed_transient"),
        ):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        storage.upsert_mcp_pending_consent.assert_not_called()
        assert ("u1", "pool-srv") not in mgr._token_sweep_warned  # retryable, not surfaced

    def test_recovery_rearms_and_clears_badge(self, running_loop_mgr, storage) -> None:
        """A dead grant that later returns healthy clears its warned pin AND drops
        the stale badge — the self-heal for a spurious invalid_grant that has
        since recovered. Production-reachable now that the observe-only sweep no
        longer deletes the row on refresh_failed, so the pair keeps enumerating."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        _seed_user_token(storage, cipher, user_id="u1", server_name="pool-srv")
        storage.delete_mcp_pending_consent = MagicMock(return_value=True)  # type: ignore[method-assign]
        key = ("u1", "pool-srv")

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=self._classified("refresh_failed"),
        ):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        assert key in mgr._token_sweep_warned
        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=self._classified("token", token="access-aaa"),
        ):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        assert key not in mgr._token_sweep_warned  # recovered → re-armed
        storage.delete_mcp_pending_consent.assert_called_once_with("u1", "pool-srv")

    def test_dead_grant_not_pinned_when_badge_persist_fails(
        self, running_loop_mgr, storage
    ) -> None:
        """If the badge write fails, the pair is NOT pinned, so the next tick
        retries — a single failed persist must not permanently lose the only
        proactive signal for a sweep-detected dead grant."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        _seed_user_token(storage, cipher, user_id="u1", server_name="pool-srv")
        storage.upsert_mcp_pending_consent = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("db down")
        )
        key = ("u1", "pool-srv")

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=self._classified("refresh_failed"),
        ):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
            assert key not in mgr._token_sweep_warned  # not pinned — will retry
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        # Retried on the second tick rather than deduped away by a phantom pin.
        assert storage.upsert_mcp_pending_consent.call_count == 2

    def test_sweep_uses_non_revoking_observe_mode(self, running_loop_mgr, storage) -> None:
        """The background sweep MUST call the canonical lookup non-destructively:
        a timer may never delete a token or move a foreground user's revoke
        threshold."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        _seed_user_token(storage, cipher, user_id="u1", server_name="pool-srv")
        seen_kwargs: list[dict[str, Any]] = []

        async def _spy(**kwargs: Any) -> Any:
            seen_kwargs.append(kwargs)
            return SimpleNamespace(kind="token", token="access-aaa")

        with patch("turnstone.core.mcp_client.get_user_access_token_classified", new=_spy):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        assert seen_kwargs and seen_kwargs[0]["revoke_on_failure"] is False
        assert seen_kwargs[0]["revoke_ambiguous_escalation"] is False

    # -- keepalive refresh (exercise the refresh token before it idles out) ---

    def test_keepalive_refresh_due_logic(self) -> None:
        mgr = MCPClientManager({})
        mgr._user_token_refresh_keepalive_s = 3600.0
        old = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        recent = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        assert mgr._keepalive_refresh_due(old) is True  # past the window → force
        assert mgr._keepalive_refresh_due(recent) is False  # still warm
        assert mgr._keepalive_refresh_due(None) is True  # unknown → force once, safe
        assert mgr._keepalive_refresh_due("not-a-date") is True  # unparseable → force
        mgr._user_token_refresh_keepalive_s = 0.0
        assert mgr._keepalive_refresh_due(old) is False  # disabled → never force

    def test_keepalive_due_forces_refresh(self, running_loop_mgr, storage) -> None:
        """A grant whose refresh token has idled past the window is force-refreshed
        even though its access token may be fresh — the [6] fix: keep the refresh
        token alive so an unattended run never finds it aged out."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        mgr._user_token_refresh_keepalive_s = 1800.0
        stale = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        storage.list_mcp_user_token_reconcile_targets = MagicMock(  # type: ignore[method-assign]
            return_value=[("u1", "pool-srv", stale)]
        )
        seen_kwargs: list[dict[str, Any]] = []

        async def _spy(**kwargs: Any) -> Any:
            seen_kwargs.append(kwargs)
            return SimpleNamespace(kind="token", token="access-aaa")

        with patch("turnstone.core.mcp_client.get_user_access_token_classified", new=_spy):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        assert seen_kwargs and seen_kwargs[0]["force_refresh"] is True

    def test_keepalive_not_due_does_not_force(self, running_loop_mgr, storage) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        mgr._user_token_refresh_keepalive_s = 1800.0
        recent = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        storage.list_mcp_user_token_reconcile_targets = MagicMock(  # type: ignore[method-assign]
            return_value=[("u1", "pool-srv", recent)]
        )
        seen_kwargs: list[dict[str, Any]] = []

        async def _spy(**kwargs: Any) -> Any:
            seen_kwargs.append(kwargs)
            return SimpleNamespace(kind="token", token="access-aaa")

        with patch("turnstone.core.mcp_client.get_user_access_token_classified", new=_spy):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        assert seen_kwargs and seen_kwargs[0]["force_refresh"] is False  # still warm

    def test_warned_set_pruned_to_consented_pairs(self, running_loop_mgr, storage) -> None:
        """A warned pair that is no longer consented (row gone) is dropped from
        the dedup set so it can't grow unbounded across transient dead grants."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        _seed_user_token(storage, cipher, user_id="u1", server_name="pool-srv")
        mgr._token_sweep_warned = {("gone-user", "pool-srv"), ("u1", "pool-srv")}

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            new=self._classified("token", token="access-aaa"),
        ):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        assert ("gone-user", "pool-srv") not in mgr._token_sweep_warned  # pruned
        assert ("u1", "pool-srv") not in mgr._token_sweep_warned  # healthy → cleared

    def test_per_pair_failure_isolated(self, running_loop_mgr, storage) -> None:
        """One pair raising must not starve the rest of the pass."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        mgr._oauth_user_server_names = {"pool-srv"}
        _seed_user_token(storage, cipher, user_id="u-bad", server_name="pool-srv")
        _seed_user_token(storage, cipher, user_id="u-ok", server_name="pool-srv")
        seen: list[str] = []

        async def _flaky(**kwargs: Any) -> Any:
            uid = kwargs["user_id"]
            seen.append(uid)
            if uid == "u-bad":
                raise RuntimeError("boom")
            return SimpleNamespace(kind="token", token="access-aaa")

        with patch("turnstone.core.mcp_client.get_user_access_token_classified", new=_flaky):
            _run_on_loop(loop, mgr._sweep_user_token_freshness())
        assert {"u-bad", "u-ok"} <= set(seen)  # both attempted despite one raising

    def test_sweep_loop_cancel_returns_cleanly(self, running_loop_mgr) -> None:
        """The loop body exits on cancellation without raising (mirrors the
        eviction loop's teardown contract)."""
        mgr, loop, _ = running_loop_mgr
        mgr._user_token_sweep_s = 999.0  # park in the sleep

        async def _spawn() -> asyncio.Task[None]:
            return asyncio.ensure_future(mgr._user_token_sweep_loop())

        task = _run_on_loop(loop, _spawn())

        async def _cancel() -> None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

        _run_on_loop(loop, _cancel())
        assert task.cancelled() or task.done()

    def test_connect_all_starts_the_sweep_task(self, running_loop_mgr) -> None:
        """Wiring guard: ``_connect_all`` must start the sweep once, even with no
        servers configured — otherwise the whole keep-hot mechanism is dead code."""
        mgr, loop, _ = running_loop_mgr
        assert mgr._user_token_sweep_task is None

        _run_on_loop(loop, mgr._connect_all())
        try:
            task = mgr._user_token_sweep_task
            assert task is not None and not task.done()  # live, single instance
        finally:

            async def _drain() -> None:
                t = mgr._user_token_sweep_task
                if t is not None:
                    t.cancel()
                    with contextlib.suppress(BaseException):
                        await t
                    mgr._user_token_sweep_task = None

            _run_on_loop(loop, _drain())

    def test_disabled_sweep_not_started_by_connect_all(self, running_loop_mgr) -> None:
        """Cadence <= 0 disables the sweep entirely — no task is spawned."""
        mgr, loop, _ = running_loop_mgr
        mgr._user_token_sweep_s = 0.0
        _run_on_loop(loop, mgr._connect_all())
        assert mgr._user_token_sweep_task is None

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (0, 0.0),  # explicit disable
            (-5, 0.0),  # negative disables (no busy-loop)
            (1, 30.0),  # tiny positive floored to _MIN_USER_TOKEN_SWEEP_S
            (600, 600.0),  # normal value passes through
        ],
    )
    def test_cadence_clamped_or_disabled(self, configured, expected) -> None:
        """The config cadence is floored (positive) or disabled (<= 0) so an
        ``asyncio.sleep(0)`` busy-loop is unreachable."""
        with patch(
            "turnstone.core.mcp_client.load_config",
            return_value={"user_token_sweep_seconds": configured},
        ):
            mgr = MCPClientManager({})
        assert mgr._user_token_sweep_s == expected

    # -- storage enumerator --------------------------------------------------

    def test_reconcile_targets_pairs_expiry_unfiltered_with_last_exercised(self, storage) -> None:
        cipher = make_mcp_token_cipher()
        # alice consents to two servers → two rows.
        _seed_user_token(storage, cipher, user_id="alice", server_name="srv-a")
        _seed_user_token(storage, cipher, user_id="alice", server_name="srv-b")
        # bob's access token is expired but the refresh token is live — still a
        # consented, reconcilable grant, so bob must be enumerated.
        _seed_user_token(
            storage, cipher, user_id="bob", server_name="srv-a", expires_in_seconds=-999
        )
        targets = storage.list_mcp_user_token_reconcile_targets()
        # (user, server) identity, all three grants present regardless of expiry.
        assert sorted((u, s) for u, s, _ in targets) == [
            ("alice", "srv-a"),
            ("alice", "srv-b"),
            ("bob", "srv-a"),
        ]
        # last_exercised = COALESCE(last_refreshed, created); never-refreshed rows
        # fall back to created, so it is always populated (drives the keepalive).
        assert all(last_exercised for _, _, last_exercised in targets)
