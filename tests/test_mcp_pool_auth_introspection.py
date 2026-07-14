"""Phase 6 unit tests — pool dispatch auth introspection (carrier + retry).

Covers the ``_classify_failure`` split into ``auth_401`` / ``auth_403``,
the response-hook carrier reset / leak guarantees, the
``force_refresh=True`` semantics in
:func:`get_user_access_token_classified`, the dispatcher's
refresh-and-retry-once policy on 401, and the
``mcp_insufficient_scope`` emission on 403. Parser-helper coverage lives
in ``tests/test_mcp_http_parsers.py``.

Negative-test verifications (run + revert + run + restore + restore):
several tests note explicit "verified by reverting [production line] to
[no-op]" lines. This bakes the Phase 5 fix-up workflow into the test
authoring discipline so future readers can re-verify the assertions
hold for the right reason.

Direct ``httpx.HTTPStatusError`` injection appears ONLY in the
classification tests for defense-in-depth coverage of the non-SDK
refresh path. The dispatcher-asserting tests drive the carrier through
the production path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tests.conftest import _run_on_loop, make_mcp_token_cipher, stop_loop_thread
from turnstone.core.mcp_client import (
    MCPClientManager,
    _AuthCapture,
    _make_capturing_http_factory,
)
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Fixtures and helpers (mirror conventions in test_mcp_user_pool.py)
# ---------------------------------------------------------------------------


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
    """Background mcp-loop fixture mirroring test_mcp_user_pool.py."""
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
        stop_loop_thread(loop, thread)


# ---------------------------------------------------------------------------
# _classify_failure with capture vs legacy fallback
# ---------------------------------------------------------------------------


class TestClassifyFailureWithCapture:
    def test_classify_failure_auth_401_with_capture(self) -> None:
        mgr = MCPClientManager({})
        capture = _AuthCapture(status=401)
        # Exception type doesn't matter — capture wins.
        assert mgr._classify_failure(RuntimeError("any"), capture=capture) == "auth_401"

    def test_classify_failure_auth_403_with_capture(self) -> None:
        mgr = MCPClientManager({})
        capture = _AuthCapture(status=403)
        assert mgr._classify_failure(RuntimeError("any"), capture=capture) == "auth_403"

    def test_classify_failure_no_capture_falls_through_to_legacy(self) -> None:
        """Defense-in-depth: ``_refresh_and_persist`` raises ``HTTPStatusError``
        directly without going through the SDK swallow, so the legacy branch
        must still classify."""
        mgr = MCPClientManager({})
        req = httpx.Request("POST", "https://mcp.example.com/sse")
        for status, label in ((401, "auth_401"), (403, "auth_403")):
            resp = httpx.Response(status, request=req)
            exc = httpx.HTTPStatusError("err", request=req, response=resp)
            assert mgr._classify_failure(exc, capture=None) == label

    def test_classify_failure_capture_with_unrelated_status_falls_through(self) -> None:
        """Carrier with status=500 (not 401/403) doesn't classify as auth."""
        mgr = MCPClientManager({})
        capture = _AuthCapture(status=500)
        # The carrier's status isn't 401 or 403, and the exception is generic.
        assert mgr._classify_failure(ValueError("nope"), capture=capture) == "other"


# ---------------------------------------------------------------------------
# Tests 9-10: carrier field-reset isolation + hook only fires on 4xx
# ---------------------------------------------------------------------------


class TestCarrierLifecycle:
    def test_capture_resets_per_dispatch(self, running_loop_mgr, storage: SQLiteBackend) -> None:
        """The carrier's fields are reset before each ``call_tool`` so a
        prior dispatch's 401 cannot leak into the next dispatch's
        classification.

        The carrier is owned by the pool entry (the httpx response hook
        closes over ``entry.auth_capture`` at first connect; a per-
        dispatch carrier would never reach the hook on a reused
        session). Object identity is
        therefore expected to be the SAME across dispatches; what
        matters is that the FIELDS are reset under ``open_lock``
        before ``call_tool`` runs.

        Test shape:
        1. Dispatch 1's stub ``call_tool`` populates the carrier with a
           401 + WWW-Authenticate.
        2. Dispatch 2's stub ``call_tool`` records the carrier's state
           as observed AT CALL ENTRY — before this stub writes anything.
        3. Assert the recorded state is reset (status=None,
           www_authenticate=None) — proves dispatch 1's payload did
           not leak.

        Verified by reverting ``_dispatch_pool_with_entry`` to remove
        the two reset assignments under ``open_lock`` and confirming
        this test fails because dispatch 2 observes dispatch 1's
        leaked 401.
        """
        from unittest.mock import patch

        from turnstone.core.mcp_oauth import TokenLookupResult

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        mgr.set_storage(storage)
        mgr.set_app_state(_make_app_state(storage, cipher=cipher))

        # Dispatch 1's stub leaves a 401 on the carrier; dispatch 2's
        # stub records carrier state at entry.
        observed_states: list[tuple[int | None, str | None]] = []
        call_index = [0]

        async def _call_tool(name: str, args: dict[str, Any]) -> Any:
            cap = getattr(mgr, "_test_active_capture", None)
            assert cap is not None, "test setup error: capture not stashed"
            observed_states.append((cap.status, cap.www_authenticate))
            if call_index[0] == 0:
                # Simulate dispatch 1 seeing a 401 — leak this into
                # the carrier so dispatch 2 must explicitly reset.
                cap.status = 401
                cap.www_authenticate = 'Bearer error="invalid_token"'
            call_index[0] += 1
            content = MagicMock()
            content.text = "ok"
            res = MagicMock()
            res.content = [content]
            res.isError = False
            return res

        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            sess = MagicMock()
            sess.call_tool = _call_tool
            entry.session = sess

        _run_on_loop(loop, _seed())

        async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
            return TokenLookupResult(kind="token", token="access-aaa")

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ):
            mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5)
            mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5)

        assert len(observed_states) == 2
        # Dispatch 1 saw the freshly-constructed carrier (None/None) —
        # initial state from PoolEntryState's default_factory.
        assert observed_states[0] == (None, None), (
            f"dispatch 1 saw stale carrier state: {observed_states[0]}"
        )
        # Dispatch 2 must observe the reset — dispatch 1's leaked 401
        # is gone.
        assert observed_states[1] == (None, None), (
            f"dispatch 2 leaked dispatch 1's state: {observed_states[1]}; "
            "_dispatch_pool_with_entry's reset of entry.auth_capture is broken"
        )

    def test_capture_dataclass_isolated_when_constructed_separately(self) -> None:
        """Sanity check: independent ``_AuthCapture`` instances do not
        share mutable state. Pure dataclass shape verification."""
        cap_a = _AuthCapture()
        cap_b = _AuthCapture()
        cap_a.status = 401
        cap_a.www_authenticate = 'Bearer error="invalid_token"'
        assert cap_b.status is None
        assert cap_b.www_authenticate is None
        cap_b.status = 403
        assert cap_a.status == 401

    @pytest.mark.anyio
    async def test_response_hook_captures_4xx_only(self) -> None:
        """Hook records on 401 and 403 only; 200/201/202/500 are ignored.

        Verified by removing the ``status in (401, 403)`` guard in
        ``_make_capturing_http_factory._hook`` and confirming that
        ``status=200`` populates the carrier (test fails because we
        assert ``capture.status is None`` for 200).

        The hook is ``async`` (httpx invokes ``await hook(response)``),
        so the test awaits it directly via the ``event_hooks`` slot.
        """
        capture = _AuthCapture()
        factory = _make_capturing_http_factory(capture)
        client = factory()
        try:
            hooks = client.event_hooks["response"]
            assert len(hooks) == 1
            hook = hooks[0]

            req = httpx.Request("POST", "https://mcp.example.com/")
            for ignored_status in (200, 201, 202, 500):
                resp = httpx.Response(
                    ignored_status,
                    request=req,
                    headers={"www-authenticate": "Bearer should-be-ignored"},
                )
                await hook(resp)
                assert capture.status is None, (
                    f"hook recorded on {ignored_status}; expected only 401/403"
                )

            for tracked_status in (401, 403):
                capture.status = None
                capture.www_authenticate = None
                resp = httpx.Response(
                    tracked_status,
                    request=req,
                    headers={"www-authenticate": f'Bearer error="x{tracked_status}"'},
                )
                await hook(resp)
                assert capture.status == tracked_status
                assert capture.www_authenticate == f'Bearer error="x{tracked_status}"'
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Test 11-12: force_refresh=True semantics in get_user_access_token_classified
# ---------------------------------------------------------------------------


class TestForceRefresh:
    @pytest.mark.anyio
    async def test_force_refresh_bypasses_token_freshness_check(
        self, storage: SQLiteBackend
    ) -> None:
        """``force_refresh=True`` MUST go through the lock + AS round-trip
        even when ``_token_needs_refresh`` returns False.

        Verified by replacing the ``force_refresh`` parameter with a
        no-op default (i.e., dropping the ``not force_refresh and``
        guard so the fast path always wins) and confirming this test
        fails — the call returns the original cached token instead of
        the refreshed value.
        """
        from unittest.mock import patch

        from turnstone.core.mcp_oauth import get_user_access_token_classified

        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="srv-oauth")
        state = _make_app_state(storage, cipher=cipher)
        # Token expires 1 hour from now — _token_needs_refresh returns False.
        _seed_user_token(
            storage, cipher, user_id="user-1", server_name="srv-oauth", expires_in_seconds=3600
        )

        async def _fake_refresh_and_persist(**_kwargs: Any) -> tuple[str, str | None, str | None]:
            return ("refreshed-token", "rotated-rrr", None)

        with patch(
            "turnstone.core.mcp_oauth._refresh_and_persist",
            side_effect=_fake_refresh_and_persist,
        ):
            # Without force_refresh: returns the cached token.
            cached = await get_user_access_token_classified(
                app_state=state, user_id="user-1", server_name="srv-oauth"
            )
            assert cached.kind == "token"
            assert cached.token == "access-aaa"
            # With force_refresh: forces the AS round-trip.
            forced = await get_user_access_token_classified(
                app_state=state,
                user_id="user-1",
                server_name="srv-oauth",
                force_refresh=True,
            )
            assert forced.kind == "token"
            assert forced.token == "refreshed-token"

    @pytest.mark.anyio
    async def test_force_refresh_collapses_concurrent_callers_via_lock(
        self, storage: SQLiteBackend
    ) -> None:
        """Two concurrent ``force_refresh=True`` callers MUST collapse to
        one AS round-trip via the dual-layer lock + the timestamp-comparison
        guard inside the locked block.

        Verified by removing the timestamp-comparison guard inside the
        ``async with lock, pg_lock:`` block (i.e., letting the second
        caller refresh again because its ``not _token_needs_refresh``
        check still has ``force_refresh=True``) and confirming this
        test counts 2 AS round-trips instead of 1.
        """
        from unittest.mock import patch

        from turnstone.core.mcp_oauth import get_user_access_token_classified

        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="srv-oauth")
        state = _make_app_state(storage, cipher=cipher)
        _seed_user_token(
            storage, cipher, user_id="user-1", server_name="srv-oauth", expires_in_seconds=3600
        )

        call_count = 0
        call_count_lock = threading.Lock()

        async def _fake_refresh_and_persist(**kwargs: Any) -> tuple[str, str | None, str | None]:
            nonlocal call_count
            with call_count_lock:
                call_count += 1
            # Yield once so the second concurrent caller can hit the
            # lock while the first is still inside.
            await asyncio.sleep(0.05)
            # Mirror what the real ``_refresh_and_persist`` does so the
            # timestamp-comparison guard inside the locked block sees a
            # fresh ``last_refreshed`` and the second caller short-circuits.
            token_store: MCPTokenStore = kwargs["token_store"]
            user_id = kwargs["user_id"]
            server_name = kwargs["server_name"]
            future_expires = (datetime.now(UTC) + timedelta(seconds=3600)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            await asyncio.to_thread(
                token_store.update_user_token_after_refresh,
                user_id,
                server_name,
                access_token="refreshed-token",
                refresh_token="rotated-rrr",
                expires_at=future_expires,
            )
            return ("refreshed-token", "rotated-rrr", future_expires)

        with patch(
            "turnstone.core.mcp_oauth._refresh_and_persist",
            side_effect=_fake_refresh_and_persist,
        ):
            results = await asyncio.gather(
                get_user_access_token_classified(
                    app_state=state,
                    user_id="user-1",
                    server_name="srv-oauth",
                    force_refresh=True,
                ),
                get_user_access_token_classified(
                    app_state=state,
                    user_id="user-1",
                    server_name="srv-oauth",
                    force_refresh=True,
                ),
            )

        for r in results:
            assert r.kind == "token"
        assert call_count == 1, (
            f"Expected the in-process lock + timestamp guard to collapse two "
            f"concurrent force_refresh callers to one AS round-trip, but "
            f"observed {call_count} round-trips."
        )

    @pytest.mark.anyio
    async def test_force_refresh_goes_through_pg_refresh_lock_path(
        self, storage: SQLiteBackend
    ) -> None:
        """``force_refresh=True`` paths MUST still flow through the
        ``_PgRefreshLock`` infrastructure (hard invariants 11-13 unchanged).
        """
        from unittest.mock import patch

        from turnstone.core.mcp_oauth import get_user_access_token_classified

        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="srv-oauth")
        state = _make_app_state(storage, cipher=cipher)
        _seed_user_token(
            storage, cipher, user_id="user-1", server_name="srv-oauth", expires_in_seconds=3600
        )

        acquired = False

        original_acquire = storage.acquire_advisory_lock_sync

        @contextlib.contextmanager
        def _tracking_acquire(key_text: str = "") -> Any:
            nonlocal acquired
            with original_acquire(key_text):
                acquired = True
                yield

        async def _fake_refresh_and_persist(**_kwargs: Any) -> tuple[str, str | None, str | None]:
            return ("refreshed-token", "rotated-rrr", None)

        with (
            patch.object(storage, "acquire_advisory_lock_sync", side_effect=_tracking_acquire),
            patch(
                "turnstone.core.mcp_oauth._refresh_and_persist",
                side_effect=_fake_refresh_and_persist,
            ),
        ):
            result = await get_user_access_token_classified(
                app_state=state,
                user_id="user-1",
                server_name="srv-oauth",
                force_refresh=True,
            )

        assert result.kind == "token"
        assert acquired, (
            "force_refresh=True bypassed the _PgRefreshLock advisory-lock path; "
            "hard invariant 13 violated."
        )


# ---------------------------------------------------------------------------
# Test 13-17: dispatcher refresh-and-retry + 403 step-up emission
# ---------------------------------------------------------------------------


class TestDispatcherAuthFlows:
    """The dispatcher consults the carrier and routes to refresh-and-retry
    (401) or structured-error emission (403)."""

    def _wire_pool(
        self,
        mgr: MCPClientManager,
        storage: SQLiteBackend,
        cipher: Any,
    ) -> SimpleNamespace:
        mgr.set_storage(storage)
        state = _make_app_state(storage, cipher=cipher)
        mgr.set_app_state(state)
        return state

    def _seed_pool_entry_with_call_tool(
        self,
        mgr: MCPClientManager,
        loop: asyncio.AbstractEventLoop,
        call_tool: Any,
    ) -> None:
        """Seed a pool entry with a fake session whose call_tool is *call_tool*.

        Also patches ``_connect_one_pool`` so the retry path (which the
        dispatcher exercises after dropping the session on auth failure)
        can re-install the same fake session without going through the
        real TCP probe / SDK handshake. The patch reinstates the same
        fake on every reconnect — the test's ``call_tool`` stub is
        responsible for varying behaviour across calls.
        """

        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            sess = MagicMock()
            sess.call_tool = call_tool
            entry.session = sess

        _run_on_loop(loop, _seed())

        async def _fake_connect(
            self_inner: MCPClientManager,
            key: tuple[str, str],
            cfg: dict[str, Any],
            access_token: str,
            *,
            auth_capture: Any = None,
            auth_fired_event: Any = None,
        ) -> Any:
            entry = await self_inner._ensure_pool_entry(key)
            sess = MagicMock()
            sess.call_tool = call_tool
            entry.session = sess
            return entry

        # Install via setattr — monkeypatching the bound method on the
        # instance avoids leaking into other tests that share the class.
        mgr._connect_one_pool = _fake_connect.__get__(mgr, type(mgr))  # type: ignore[method-assign]

    def test_dispatch_pool_401_refreshes_and_retries(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """First call sees carrier=401, dispatcher signals retry; the
        retry's ``_dispatch_pool`` reads the token with ``force_refresh=True``
        (via ``retry_count > 0``) and the second call returns success.

        Verified by reverting ``_dispatch_pool_sync`` to remove the
        ``except _PoolDispatchRetryRequested`` block (so the signal
        propagates instead of triggering the retry on a fresh task)
        and confirming this test fails because the retry never fires.
        """
        from unittest.mock import patch

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        self._wire_pool(mgr, storage, cipher)

        from turnstone.core.mcp_oauth import TokenLookupResult

        call_count = 0

        # Stub force_refresh to return a fresh token.
        async def _fake_classified(
            **kwargs: Any,
        ) -> TokenLookupResult:
            if kwargs.get("force_refresh"):
                return TokenLookupResult(kind="token", token="refreshed-bearer")
            return TokenLookupResult(kind="token", token="access-aaa")

        # The first call_tool populates the entry-owned carrier with
        # 401 (via _populate_active_capture, simulating what the
        # production response hook would record); the second call_tool
        # returns success.
        async def _call_tool(name: str, args: dict[str, Any]) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate the SDK swallow path: carrier was populated
                # by the hook, but the exception that surfaces is a
                # generic CONNECTION_CLOSED-shaped error.
                _populate_active_capture(mgr, status=401, header='Bearer error="invalid_token"')
                raise RuntimeError("upstream 401 (SDK-swallow shape)")
            content = MagicMock()
            content.text = "ok"
            res = MagicMock()
            res.content = [content]
            res.isError = False
            return res

        self._seed_pool_entry_with_call_tool(mgr, loop, _call_tool)

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ):
            result = mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )

        assert result == "ok"
        assert call_count == 2, (
            f"Expected exactly 2 call_tool invocations (initial + retry); got {call_count}"
        )
        # Auth failures must not affect the breaker (hard invariant 3).
        assert mgr._consecutive_failures.get("pool-srv", 0) == 0

    def test_dispatch_pool_401_retry_with_refresh_failure_emits_consent_required(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """When the AS rejects the refresh, the dispatcher emits
        ``mcp_consent_required`` (no exception, no breaker tick)."""
        from unittest.mock import patch

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        self._wire_pool(mgr, storage, cipher)

        from turnstone.core.mcp_oauth import TokenLookupResult

        async def _fake_classified(
            **kwargs: Any,
        ) -> TokenLookupResult:
            if kwargs.get("force_refresh"):
                return TokenLookupResult(kind="refresh_failed")
            return TokenLookupResult(kind="token", token="access-aaa")

        async def _call_tool(name: str, args: dict[str, Any]) -> Any:
            _populate_active_capture(mgr, status=401, header='Bearer error="invalid_token"')
            raise RuntimeError("upstream 401")

        self._seed_pool_entry_with_call_tool(mgr, loop, _call_tool)

        with (
            patch(
                "turnstone.core.mcp_client.get_user_access_token_classified",
                side_effect=_fake_classified,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_consent_required"
        assert payload["error"]["server"] == "pool-srv"
        assert mgr._consecutive_failures.get("pool-srv", 0) == 0

    def test_dispatch_pool_transient_refresh_emits_retryable_not_consent(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """A TRANSIENT refresh failure on the 401-retry surfaces a retryable
        ``mcp_refresh_unavailable`` error — NOT a re-consent prompt — and does
        not tick the breaker."""
        from unittest.mock import patch

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        self._wire_pool(mgr, storage, cipher)

        from turnstone.core.mcp_oauth import TokenLookupResult

        async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
            if kwargs.get("force_refresh"):
                return TokenLookupResult(kind="refresh_failed_transient")
            return TokenLookupResult(kind="token", token="access-aaa")

        async def _call_tool(name: str, args: dict[str, Any]) -> Any:
            _populate_active_capture(mgr, status=401, header='Bearer error="invalid_token"')
            raise RuntimeError("upstream 401")

        self._seed_pool_entry_with_call_tool(mgr, loop, _call_tool)

        with (
            patch(
                "turnstone.core.mcp_client.get_user_access_token_classified",
                side_effect=_fake_classified,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5)

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_refresh_unavailable"
        assert payload["error"]["server"] == "pool-srv"
        assert mgr._consecutive_failures.get("pool-srv", 0) == 0

    def test_dispatch_pool_401_retry_ceiling_caps_at_one(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """Even when the refresh succeeds, if the retry ALSO 401s, the
        dispatcher emits ``mcp_consent_required`` rather than recursing.

        Verified by editing ``_dispatch_pool`` to relax the
        ``if retry_count == 0`` guard (e.g. to ``if retry_count <= 1``)
        so the auth_401 branch raises ``_PoolDispatchRetryRequested``
        even at the ceiling. ``_dispatch_pool_sync`` only catches the
        signal once, so the second raise propagates back to
        ``call_tool_sync`` and the test fails on the missing
        ``mcp_consent_required`` payload (``json.loads`` on a
        non-JSON / raised result). The point of the negative-test is
        to prove the ceiling (``retry_count == 0`` guard) is what
        bounds the retry loop — without it, the dispatcher would loop
        the bearer-rejection cycle indefinitely.
        """
        from unittest.mock import patch

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        self._wire_pool(mgr, storage, cipher)

        from turnstone.core.mcp_oauth import TokenLookupResult

        async def _fake_classified(
            **kwargs: Any,
        ) -> TokenLookupResult:
            return TokenLookupResult(kind="token", token="bearer-XYZ")

        call_count = 0

        async def _call_tool(name: str, args: dict[str, Any]) -> Any:
            nonlocal call_count
            call_count += 1
            _populate_active_capture(mgr, status=401, header='Bearer error="invalid_token"')
            raise RuntimeError("upstream 401")

        self._seed_pool_entry_with_call_tool(mgr, loop, _call_tool)

        with (
            patch(
                "turnstone.core.mcp_client.get_user_access_token_classified",
                side_effect=_fake_classified,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_consent_required"
        # Exactly TWO calls: initial + retry. No recursion.
        assert call_count == 2, (
            f"Expected exactly 2 call_tool invocations (initial + 1 retry); got {call_count}"
        )

    def test_dispatch_pool_403_emits_insufficient_scope_with_parsed_scopes(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """403 + ``error="insufficient_scope"`` emits ``mcp_insufficient_scope``
        with the parsed scope set; no retry."""
        from unittest.mock import patch

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        self._wire_pool(mgr, storage, cipher)

        from turnstone.core.mcp_oauth import TokenLookupResult

        async def _fake_classified(
            **_kwargs: Any,
        ) -> TokenLookupResult:
            return TokenLookupResult(kind="token", token="access-aaa")

        call_count = 0

        async def _call_tool(name: str, args: dict[str, Any]) -> Any:
            nonlocal call_count
            call_count += 1
            _populate_active_capture(
                mgr,
                status=403,
                header='Bearer error="insufficient_scope", scope="files:write mail:send"',
            )
            raise RuntimeError("upstream 403")

        self._seed_pool_entry_with_call_tool(mgr, loop, _call_tool)

        with (
            patch(
                "turnstone.core.mcp_client.get_user_access_token_classified",
                side_effect=_fake_classified,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_insufficient_scope"
        assert payload["error"]["scopes_required"] == ["files:write", "mail:send"]
        assert call_count == 1, "403 must NOT trigger a retry"
        assert mgr._consecutive_failures.get("pool-srv", 0) == 0

    def test_dispatch_pool_403_without_insufficient_scope_emits_generic_forbidden(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """403 without ``error="insufficient_scope"`` → generic forbidden."""
        from unittest.mock import patch

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        self._wire_pool(mgr, storage, cipher)

        from turnstone.core.mcp_oauth import TokenLookupResult

        async def _fake_classified(
            **_kwargs: Any,
        ) -> TokenLookupResult:
            return TokenLookupResult(kind="token", token="access-aaa")

        async def _call_tool(name: str, args: dict[str, Any]) -> Any:
            _populate_active_capture(mgr, status=403, header="Bearer realm=mcp")
            raise RuntimeError("upstream 403")

        self._seed_pool_entry_with_call_tool(mgr, loop, _call_tool)

        with (
            patch(
                "turnstone.core.mcp_client.get_user_access_token_classified",
                side_effect=_fake_classified,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mgr.call_tool_sync(
                "mcp__pool-srv__do_thing",
                {},
                user_id="user-1",
                timeout=5,
            )

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_tool_call_forbidden"
        assert "scopes_required" not in payload["error"]


# ---------------------------------------------------------------------------
# Test 18: hard invariant 3 — auth failures never trip the breaker
# ---------------------------------------------------------------------------


class TestBreakerInvariant:
    def test_auth_failures_do_not_trip_breaker(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """After 401-then-success and after 403-emit, the per-server breaker
        counter MUST remain 0 (hard invariant 3).

        Verified by adding ``self._cb_record_failure(server_name)`` to
        the ``auth_401`` and ``auth_403`` branches of ``_dispatch_pool``
        and confirming this test fails because the counter advances.
        """
        from unittest.mock import patch

        from turnstone.core.mcp_oauth import TokenLookupResult

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        mgr.set_storage(storage)
        mgr.set_app_state(_make_app_state(storage, cipher=cipher))

        async def _fake_classified(**kwargs: Any) -> TokenLookupResult:
            if kwargs.get("force_refresh"):
                return TokenLookupResult(kind="token", token="bearer-refreshed")
            return TokenLookupResult(kind="token", token="bearer-original")

        # Multi-stage call_tool: 401 (initial) → ok (retry) → 403 (next dispatch).
        # The 403 step doesn't retry so it's the third element.
        async def _call_tool_401(name: str, args: dict[str, Any]) -> Any:
            _populate_active_capture(mgr, status=401, header='Bearer error="invalid_token"')
            raise RuntimeError("upstream 401")

        async def _call_tool_success(name: str, args: dict[str, Any]) -> Any:
            content = MagicMock()
            content.text = "ok"
            res = MagicMock()
            res.content = [content]
            res.isError = False
            return res

        async def _call_tool_403(name: str, args: dict[str, Any]) -> Any:
            _populate_active_capture(
                mgr, status=403, header='Bearer error="insufficient_scope", scope="x:y"'
            )
            raise RuntimeError("upstream 403")

        seq = iter([_call_tool_401, _call_tool_success, _call_tool_403])

        async def _staged_call_tool(name: str, args: dict[str, Any]) -> Any:
            fn = next(seq)
            return await fn(name, args)

        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            sess = MagicMock()
            sess.call_tool = _staged_call_tool
            entry.session = sess

        _run_on_loop(loop, _seed())

        # Patch _connect_one_pool to re-install the staged session on
        # reconnect (the dispatcher drops the session after auth failure).
        async def _fake_connect(
            self_inner: MCPClientManager,
            key: tuple[str, str],
            cfg: dict[str, Any],
            access_token: str,
            *,
            auth_capture: Any = None,
            auth_fired_event: Any = None,
        ) -> Any:
            entry = await self_inner._ensure_pool_entry(key)
            sess = MagicMock()
            sess.call_tool = _staged_call_tool
            entry.session = sess
            return entry

        mgr._connect_one_pool = _fake_connect.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ):
            result_401_retry = mgr.call_tool_sync(
                "mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5
            )
        assert result_401_retry == "ok"
        assert mgr._consecutive_failures.get("pool-srv", 0) == 0

        with (
            patch(
                "turnstone.core.mcp_client.get_user_access_token_classified",
                side_effect=_fake_classified,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5)
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "mcp_insufficient_scope"
        # STILL zero after the 403 cycle.
        assert mgr._consecutive_failures.get("pool-srv", 0) == 0


# ---------------------------------------------------------------------------
# Test 19: hard invariant 1 — static path does NOT receive the capture factory
# ---------------------------------------------------------------------------


class TestStaticPathUnchanged:
    def test_static_path_does_not_pass_capturing_factory(self) -> None:
        """``_connect_one`` (static path) must NEVER pass
        ``httpx_client_factory`` to ``streamablehttp_client``.

        Hard invariant 1: any change to the static-path connect plumbing
        is potentially breaking. Verified by source inspection rather
        than runtime mocking — the static path's call site is the only
        non-test ``streamablehttp_client(...)`` invocation that must
        omit the factory parameter.
        """
        import inspect

        from turnstone.core import mcp_client

        # The static path's streamablehttp_client call site lives in the
        # transport owner task (``_static_transport_owner``); ``_connect_one``
        # is a per-name-lock wrapper and ``_connect_one_locked`` only waits on
        # the owner's readiness.
        source = inspect.getsource(mcp_client.MCPClientManager._static_transport_owner)

        # The static path's streamablehttp_client invocation should NOT
        # mention ``httpx_client_factory``. Pool path keeps it.
        assert "streamablehttp_client" in source
        # The call site in the owner is bare — no factory keyword. We grep by
        # line: the factory keyword must not appear in the static-path source.
        for line in source.splitlines():
            if "httpx_client_factory" in line:
                pytest.fail(
                    "_static_transport_owner (static path) passes httpx_client_factory "
                    "to streamablehttp_client; hard invariant 1 violated."
                )


# ---------------------------------------------------------------------------
# Test 20: hold open_lock across call_tool (Phase 6 multiplex revert)
# ---------------------------------------------------------------------------


class TestOpenLockHeldAcrossCallTool:
    def test_pool_dispatch_holds_open_lock_across_call_tool(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """Phase 6 reverts Phase 5 perf-1 for the auth-aware path: hold
        ``open_lock`` across ``call_tool`` so concurrent dispatches can't
        race on the response-hook carrier.

        This test mirrors
        ``test_pool_concurrent_dispatch_to_same_user_server_is_serialized``
        in ``test_mcp_user_pool.py`` but pins the assertion to the carrier
        isolation rationale specifically.

        Verified by reverting ``_dispatch_pool_with_entry`` to release
        ``open_lock`` before ``call_tool`` (move the
        ``in_flight += 1`` / ``call_tool`` / decrement out of the
        ``async with`` body) and confirming this test observes
        ``observed_max_concurrency == 2``.
        """
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        mgr.set_storage(storage)
        mgr.set_app_state(_make_app_state(storage, cipher=cipher))

        observed_max = 0
        in_flight = 0
        in_flight_lock = threading.Lock()

        async def _call_tool(name: str, args: dict[str, Any]) -> Any:
            nonlocal observed_max, in_flight
            with in_flight_lock:
                in_flight += 1
                observed_max = max(observed_max, in_flight)
            try:
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

        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            sess = MagicMock()
            sess.call_tool = _call_tool
            entry.session = sess

        _run_on_loop(loop, _seed())

        results: list[str] = []

        def _dispatch() -> None:
            results.append(
                mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5)
            )

        t1 = threading.Thread(target=_dispatch)
        t2 = threading.Thread(target=_dispatch)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results == ["ok", "ok"]
        assert observed_max == 1, (
            "open_lock was released before call_tool, allowing concurrent "
            "carrier crosstalk between same-(user, server) dispatches."
        )


class TestCrossTaskRetryIsolation:
    """The auth_401 retry MUST run on a fresh asyncio.Task.

    Phase 6's cross-task hop (``_dispatch_pool_sync`` schedules the
    retry via a SECOND ``asyncio.run_coroutine_threadsafe`` call so the
    retry's ``streamablehttp_client`` TaskGroup gets a clean anyio
    cancel-scope state, free of the prior connect's teardown
    pollution). An in-task retry inherits the prior anyio scope across
    ``task.uncancel()`` and surfaces ``CancelledError`` from inside the
    retry's own ``streamablehttp_client`` scope (verified empirically
    by the prior implementer; cross-task is the architectural fix).
    """

    def test_dispatch_pool_sync_retries_on_fresh_task(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """Mock ``_dispatch_pool`` to raise ``_PoolDispatchRetryRequested``
        once, then succeed. Assert the two invocations ran on different
        ``asyncio.Task`` instances (proving the retry got a fresh task).

        Verified by reverting ``_dispatch_pool_sync`` to remove the
        ``except _PoolDispatchRetryRequested`` block (so the signal
        propagates instead of being caught and re-issued via a second
        ``_run_pool_dispatch_attempt``) and confirming the test fails —
        the retry never fires, so only one ``_dispatch_pool`` invocation
        is recorded and the expected success result never returns.
        """
        from turnstone.core.mcp_client import _PoolDispatchRetryRequested

        mgr, _loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        mgr.set_storage(storage)
        mgr.set_app_state(_make_app_state(storage, cipher=cipher))

        # Capture each invocation's task identity.
        invocations: list[dict[str, Any]] = []

        async def _fake_dispatch_pool(**kwargs: Any) -> str:
            task = asyncio.current_task()
            invocations.append(
                {
                    "retry_count": kwargs.get("retry_count"),
                    "task_id": id(task),
                    "task_name": task.get_name() if task else None,
                }
            )
            if kwargs.get("retry_count") == 0:
                raise _PoolDispatchRetryRequested
            return "RETRY_OK"

        mgr._dispatch_pool = _fake_dispatch_pool  # type: ignore[method-assign]

        result = mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5)

        assert result == "RETRY_OK"
        assert len(invocations) == 2, (
            f"expected 2 _dispatch_pool invocations (initial + retry); got {len(invocations)}"
        )
        assert invocations[0]["retry_count"] == 0
        assert invocations[1]["retry_count"] == 1
        assert invocations[0]["task_id"] != invocations[1]["task_id"], (
            "retry ran on the SAME asyncio.Task as the initial attempt; "
            "cross-task isolation broken — the retry's anyio cancel-scope "
            "would inherit teardown state from the prior connect."
        )


class TestRetryTimeoutBudget:
    """The retry's ``future.result(timeout=...)`` window MUST be reduced
    by however long the first attempt consumed before raising
    ``_PoolDispatchRetryRequested``.

    The earlier ``for retry_count in (0, 1):`` loop passed the full
    ``timeout`` to BOTH ``future.result`` calls. A first attempt that
    consumed almost the entire budget could therefore double the
    caller-observed timeout window (initial budget + retry budget) when
    the retry stalled. The wall-clock budget collapses both attempts
    into one ``timeout``-bounded window.

    Negative-test verification: reverting ``_dispatch_pool_sync`` to
    pass ``timeout`` (instead of ``remaining``) on the second attempt
    makes ``test_retry_attempt_timeout_reduced_by_first_attempt_duration``
    fail — the captured second-attempt timeout would equal the original.
    """

    def test_retry_attempt_timeout_reduced_by_first_attempt_duration(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """First attempt sleeps ~1s before raising the retry signal; the
        second attempt's ``future.result(timeout=...)`` MUST receive a
        value strictly less than the original ``timeout``.
        """
        from turnstone.core.mcp_client import _PoolDispatchRetryRequested

        mgr, _loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        mgr.set_storage(storage)
        mgr.set_app_state(_make_app_state(storage, cipher=cipher))

        observed_timeouts: list[int] = []

        async def _slow_then_succeed(**kwargs: Any) -> str:
            if kwargs.get("retry_count") == 0:
                # Burn ~1s of wall-clock on the first attempt before
                # signalling the retry.
                await asyncio.sleep(1.0)
                raise _PoolDispatchRetryRequested
            return "RETRY_OK"

        mgr._dispatch_pool = _slow_then_succeed  # type: ignore[method-assign]

        original_run = mgr._run_pool_dispatch_attempt

        def _spy_run(**kwargs: Any) -> str:
            observed_timeouts.append(kwargs["timeout"])
            return original_run(**kwargs)

        mgr._run_pool_dispatch_attempt = _spy_run  # type: ignore[method-assign]

        result = mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=10)

        assert result == "RETRY_OK"
        assert len(observed_timeouts) == 2, (
            f"expected 2 attempts (initial + retry); got {len(observed_timeouts)} "
            f"timeouts={observed_timeouts!r}"
        )
        # Initial attempt sees the full budget.
        assert observed_timeouts[0] == 10
        # Retry's window is reduced by the first attempt's ~1s sleep.
        assert observed_timeouts[1] < 10, (
            "retry attempt received the full original timeout instead of "
            f"the wall-clock remainder; observed_timeouts={observed_timeouts!r}"
        )

    def test_timeout_message_reports_original_budget(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """``TimeoutError`` message uses the caller's original budget,
        even when the retry's trimmed window is what actually expired.
        """
        from turnstone.core.mcp_client import _PoolDispatchRetryRequested

        mgr, _loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher)
        mgr.set_storage(storage)
        mgr.set_app_state(_make_app_state(storage, cipher=cipher))

        async def _retry_then_hang(**kwargs: Any) -> str:
            if kwargs.get("retry_count") == 0:
                raise _PoolDispatchRetryRequested
            # Hang past whatever timeout we get.
            await asyncio.sleep(60)
            return "never"

        mgr._dispatch_pool = _retry_then_hang  # type: ignore[method-assign]

        with pytest.raises(TimeoutError) as exc_info:
            mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=2)
        assert "timed out after 2s" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test helpers — bridge the test stand-in for the real httpx event hook.
#
# The dispatcher consults ``entry.auth_capture`` (carrier owned by the pool
# entry, persisted across dispatches; the production response hook closes
# over it at first connect). To unit-test the dispatcher's reaction without
# a real upstream, the fake ``call_tool`` populates that carrier directly
# via this helper, simulating what the production response hook would do.
# ---------------------------------------------------------------------------


def _populate_active_capture(mgr: MCPClientManager, *, status: int, header: str) -> None:
    """Mutate the entry-owned ``_AuthCapture`` stashed on the manager
    by the autouse ``_install_capture_intercept`` fixture.

    Used by stub ``call_tool`` implementations to simulate what the
    production response hook would record on a 4xx upstream response.
    Raises ``RuntimeError`` if invoked outside the autouse fixture's
    intercept window — the helper only resolves the carrier while a
    dispatch is in flight.
    """
    capture = getattr(mgr, "_test_active_capture", None)
    if capture is None:
        raise RuntimeError(
            "test setup error: _populate_active_capture called before "
            "_install_capture_intercept; the entry's _AuthCapture isn't "
            "observable from this stub."
        )
    capture.status = status
    capture.www_authenticate = header


@pytest.fixture(autouse=True)
def _install_capture_intercept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap ``_dispatch_pool_with_entry`` so the test's fake ``call_tool``
    can populate the entry-owned ``_AuthCapture`` via ``mgr._test_active_capture``.

    The wrap is a no-op for tests that don't call ``_populate_active_capture``;
    they simply never read the attribute. Dispatcher-asserting tests rely
    on this so the fake call_tool stub can mutate the same carrier object
    the dispatcher inspects after raising.
    """
    from turnstone.core import mcp_client as mcp_client_mod

    original = mcp_client_mod.MCPClientManager._dispatch_pool_with_entry

    async def _wrapped(self: MCPClientManager, **kwargs: Any) -> str:
        # Stash the entry's persistent carrier so the test's call_tool
        # stub can populate it.
        entry = kwargs.get("entry")
        self._test_active_capture = (  # type: ignore[attr-defined]
            entry.auth_capture if entry is not None else None
        )
        try:
            return await original(self, **kwargs)
        finally:
            self._test_active_capture = None  # type: ignore[attr-defined]

    monkeypatch.setattr(
        mcp_client_mod.MCPClientManager,
        "_dispatch_pool_with_entry",
        _wrapped,
    )


# ---------------------------------------------------------------------------
# bug-1 — pool dispatchers RAISE on structured-error envelopes
#
# Pre-fix, ``_dispatch_pool`` and ``_dispatch_pool_resource`` returned
# ``_structured_error(...)`` JSON in the success-shape return slot, which
# the public ``call_tool_sync`` / ``read_resource_sync`` then forwarded
# verbatim. ``ChatSession._exec_mcp_tool`` /  ``_exec_read_resource``
# saw a successful return, called ``_report_tool_result(..., is_error=False)``,
# and the dashboard's ``appendToolOutput`` short-circuited
# ``tryParseMcpError`` because that gate fires only inside the
# ``isError`` branch. The interactive consent card NEVER rendered.
#
# The fix wraps the dispatcher's final return: when the result is a
# structured-error envelope (``_is_structured_error``), the sync
# wrapper raises ``RuntimeError(json_str)``. The agent-loop's
# ``except Exception`` branch then calls ``_format_mcp_dispatch_error``
# which preserves the JSON on the structured-error path, and
# ``_report_tool_result(..., is_error=True)`` fires — the dashboard's
# tryParseMcpError gate opens and the card renders.
#
# These tests pin the contract at the public API. Each parametrized
# case mocks ``_dispatch_pool*`` to RETURN (not raise) the structured
# error string and asserts ``call_tool_sync`` / ``read_resource_sync``
# / ``get_prompt_sync`` raise ``RuntimeError`` carrying the JSON.
# ---------------------------------------------------------------------------


_BUG1_STRUCTURED_ERROR_CODES = (
    "mcp_consent_required",
    "mcp_insufficient_scope",
    "mcp_tool_call_forbidden",
    "mcp_token_undecryptable_key_unknown",
    "mcp_oauth_url_insecure",
)


def _make_structured_error_json(code: str, *, server: str = "pool-srv") -> str:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "server": server,
            "detail": f"test-fixture-{code}",
        }
    }
    if code == "mcp_insufficient_scope":
        payload["error"]["scopes_required"] = ["files:write"]
        payload["error"]["consent_url"] = (
            "/v1/api/mcp/oauth/start?server=pool-srv&scopes=files%3Awrite"
        )
    elif code == "mcp_consent_required":
        payload["error"]["consent_url"] = "/v1/api/mcp/oauth/start?server=pool-srv"
    return json.dumps(payload)


@pytest.mark.parametrize("code", _BUG1_STRUCTURED_ERROR_CODES)
def test_call_tool_sync_raises_on_structured_error_envelope(
    code: str, running_loop_mgr, storage: SQLiteBackend
) -> None:
    """End-to-end regression for the consent-card non-rendering bug.

    Mocks ``_dispatch_pool`` to RETURN the structured-error JSON in the
    success slot (the pre-fix production shape). The public
    ``call_tool_sync`` MUST raise ``RuntimeError`` carrying the JSON
    so the session-layer ``except Exception`` handler runs and
    ``_report_tool_result(..., is_error=True)`` fires.
    """
    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv")
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    json_payload = _make_structured_error_json(code)

    async def _fake_dispatch_pool(**_kwargs: Any) -> str:
        return json_payload

    mgr._dispatch_pool = _fake_dispatch_pool  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as exc_info:
        mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5)

    # The exception text MUST be the structured-error JSON byte-for-byte
    # (so ``_format_mcp_dispatch_error`` recognises the envelope and
    # surfaces it intact to the dashboard).
    assert str(exc_info.value) == json_payload
    decoded = json.loads(str(exc_info.value))
    assert decoded["error"]["code"] == code


@pytest.mark.parametrize("code", _BUG1_STRUCTURED_ERROR_CODES)
def test_read_resource_sync_raises_on_structured_error_envelope(
    code: str, running_loop_mgr, storage: SQLiteBackend
) -> None:
    """Mirror of the tool path's bug-1 regression for resource reads."""
    mgr, loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv")
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    # Seed the resource map so the resolver finds ``res://hello`` and
    # routes through ``_dispatch_pool_resource_sync``.
    async def _seed() -> None:
        entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
        entry.resources = [
            {
                "uri": "res://hello",
                "name": "",
                "description": "",
                "mimeType": "",
                "server": "pool-srv",
            }
        ]
        mgr._rebuild_user_resource_map("user-1")

    asyncio.run_coroutine_threadsafe(_seed(), loop).result(timeout=5)

    json_payload = _make_structured_error_json(code)

    async def _fake_dispatch_pool_resource(**_kwargs: Any) -> str:
        return json_payload

    mgr._dispatch_pool_resource = _fake_dispatch_pool_resource  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as exc_info:
        mgr.read_resource_sync("res://hello", user_id="user-1", timeout=5)

    assert str(exc_info.value) == json_payload
    decoded = json.loads(str(exc_info.value))
    assert decoded["error"]["code"] == code


@pytest.mark.parametrize("code", _BUG1_STRUCTURED_ERROR_CODES)
def test_get_prompt_sync_raises_on_structured_error_envelope(
    code: str, running_loop_mgr, storage: SQLiteBackend
) -> None:
    """Mirror of the tool path's bug-1 regression for prompt invocation.

    The prompt path already converted the structured-error string to a
    ``RuntimeError`` via the ``isinstance(result, str)`` check in
    ``_dispatch_pool_prompt_sync`` (success type is ``list[dict]`` so a
    str return is unambiguously the failure path). This test pins the
    behaviour at the public API so the contract stays uniform across
    tool / resource / prompt dispatchers post-bug-1.
    """
    mgr, loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv")
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    async def _seed() -> None:
        entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
        entry.prompts = [
            {
                "name": "mcp__pool-srv__greet",
                "original_name": "greet",
                "server": "pool-srv",
                "description": "",
                "arguments": [],
            }
        ]
        mgr._rebuild_user_prompt_map("user-1")

    asyncio.run_coroutine_threadsafe(_seed(), loop).result(timeout=5)

    json_payload = _make_structured_error_json(code)

    async def _fake_dispatch_pool_prompt(**_kwargs: Any) -> str:
        return json_payload

    mgr._dispatch_pool_prompt = _fake_dispatch_pool_prompt  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as exc_info:
        mgr.get_prompt_sync("mcp__pool-srv__greet", {}, user_id="user-1", timeout=5)

    assert str(exc_info.value) == json_payload
    decoded = json.loads(str(exc_info.value))
    assert decoded["error"]["code"] == code


def test_call_tool_sync_does_not_wrap_non_structured_string(
    running_loop_mgr, storage: SQLiteBackend
) -> None:
    """A success-shape return whose payload merely happens to start with
    ``{"error":...`` but lacks an ``mcp_*`` code MUST flow back to the
    caller as a string, not a raised RuntimeError. This is the bug-1
    fix's defensive gate: only structured-error envelopes are wrapped.
    """
    mgr, _loop, _ = running_loop_mgr
    cipher = make_mcp_token_cipher()
    _seed_oauth_server(storage, name="pool-srv")
    _seed_user_token(storage, cipher)
    mgr.set_storage(storage)
    mgr.set_app_state(_make_app_state(storage, cipher=cipher))

    # Tool output that happens to look JSON-ish but isn't a Phase 7b
    # ``_structured_error`` envelope. Must round-trip as a plain string.
    payload = json.dumps({"error": {"code": "tool_specific_failure", "msg": "x"}})

    async def _fake_dispatch_pool(**_kwargs: Any) -> str:
        return payload

    mgr._dispatch_pool = _fake_dispatch_pool  # type: ignore[method-assign]

    result = mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5)
    assert result == payload


class TestPoolPrimingAndTokenRotation:
    """Per-user pool priming (PR #706 follow-up) and the bound-token rotation
    reconnect. Priming must be NON-DESTRUCTIVE — it must never drive a token
    refresh whose transient failure would revoke consent."""

    def _wire(self, mgr: MCPClientManager, storage: SQLiteBackend, cipher: Any) -> None:
        mgr.set_storage(storage)
        mgr.set_app_state(_make_app_state(storage, cipher=cipher))
        mgr._oauth_user_server_names = {"pool-srv"}

    def test_prime_user_pools_warms_fresh_token_server(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher, expires_in_seconds=3600, access_token="bearer-fresh")
        self._wire(mgr, storage, cipher)

        primed: list[tuple[tuple[str, str], str]] = []

        async def _fake_prime(
            self_inner: MCPClientManager, key: tuple[str, str], cfg: dict[str, Any], token: str
        ) -> int:
            primed.append((key, token))
            return 3

        mgr._prime_user_server = _fake_prime.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        _run_on_loop(loop, mgr._prime_user_pools("user-1"))

        assert primed == [(("user-1", "pool-srv"), "bearer-fresh")]

    def test_prime_user_pools_refreshes_expired_token_and_warms(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """An expired/near-expiry token is now REFRESHED (via the guarded
        classified resolver) and the pool is warmed with the fresh token —
        closing the chicken-and-egg where an expired token left the pool
        permanently cold ("connecting" / no tools / never-refreshed)."""
        from unittest.mock import patch

        from turnstone.core.mcp_oauth import TokenLookupResult

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher, expires_in_seconds=5, access_token="bearer-stale")
        self._wire(mgr, storage, cipher)

        primed: list[tuple[tuple[str, str], str]] = []

        async def _fake_prime(
            self_inner: MCPClientManager, key: tuple[str, str], cfg: dict[str, Any], token: str
        ) -> int:
            primed.append((key, token))
            return 3

        mgr._prime_user_server = _fake_prime.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
            # The resolver refreshed the expired token and returns the fresh one.
            return TokenLookupResult(kind="token", token="bearer-refreshed")

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ):
            _run_on_loop(loop, mgr._prime_user_pools("user-1"))

        assert primed == [(("user-1", "pool-srv"), "bearer-refreshed")], (
            "expired token must be refreshed and the pool warmed with the fresh token"
        )

    def test_prime_user_pools_transient_refresh_failure_skips_without_revoking(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """Safety invariant preserved: a TRANSIENT refresh failure during priming
        does not warm the pool AND does not revoke — the classified resolver keeps
        the token (kind=refresh_failed_transient) and lazy dispatch retries later."""
        from unittest.mock import patch

        from turnstone.core.mcp_oauth import TokenLookupResult

        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher, expires_in_seconds=5, access_token="bearer-stale")
        self._wire(mgr, storage, cipher)

        primed: list[tuple[str, str]] = []

        async def _fake_prime(
            self_inner: MCPClientManager, key: tuple[str, str], cfg: dict[str, Any], token: str
        ) -> int:
            primed.append(key)
            return 0

        mgr._prime_user_server = _fake_prime.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        async def _fake_classified(**_kwargs: Any) -> TokenLookupResult:
            return TokenLookupResult(kind="refresh_failed_transient")

        with patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_fake_classified,
        ):
            _run_on_loop(loop, mgr._prime_user_pools("user-1"))

        assert primed == [], "transient refresh failure must not warm the pool"
        # The token row must survive — priming must never revoke on a transient blip.
        # NOTE: the resolver is stubbed here, so this only covers _prime_user_pools'
        # handling of a transient result; the actual revoke-vs-keep decision under
        # the flag prime passes is exercised by
        # test_non_destructive_resolve_keeps_dead_grant_default_revokes below.
        store = MCPTokenStore(storage, cipher, node_id="test")
        assert store.get_user_token("user-1", "pool-srv") is not None

    @pytest.mark.anyio
    async def test_prime_revokes_permanent_but_defers_ambiguous_escalation(
        self, storage: SQLiteBackend
    ) -> None:
        """Priming resolves with revoke_ambiguous_escalation=False. A PERMANENT
        rejection (invalid_grant — a reliable dead-grant signal) is STILL revoked
        so the catalog isn't stranded cold behind a phantom 'consented' token;
        only a sustained-UNCLASSIFIABLE (ambiguous) escalation is deferred to lazy
        dispatch. Drives the REAL resolver (only the AS round-trip is stubbed)."""
        from unittest.mock import patch

        from turnstone.core.mcp_oauth import (
            _AMBIGUOUS_ESCALATION_THRESHOLD,
            MCPOAuthRefreshFailed,
            _refresh_backoff_state,
            _RefreshFailureClass,
            get_user_access_token_classified,
        )

        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="srv-oauth")
        state = _make_app_state(storage, cipher=cipher)
        store = MCPTokenStore(storage, cipher, node_id="test")

        def _raiser(cls: _RefreshFailureClass) -> Any:
            async def _f(**_kwargs: Any) -> tuple[str, str | None, str | None]:
                raise MCPOAuthRefreshFailed("boom", failure_class=cls)

            return _f

        def _seed(uid: str) -> None:
            # Expired-with-refresh so each resolve reaches the refresh path.
            _seed_user_token(
                storage, cipher, user_id=uid, server_name="srv-oauth", expires_in_seconds=-10
            )

        # (1) PERMANENT during prime → REVOKED (genuinely dead → clean re-consent).
        _seed("perm-user")
        with patch(
            "turnstone.core.mcp_oauth._refresh_and_persist",
            side_effect=_raiser(_RefreshFailureClass.PERMANENT),
        ):
            perm = await get_user_access_token_classified(
                app_state=state,
                user_id="perm-user",
                server_name="srv-oauth",
                revoke_ambiguous_escalation=False,
            )
        assert perm.kind == "refresh_failed"
        assert store.get_user_token("perm-user", "srv-oauth") is None, (
            "prime must revoke a PERMANENT (reliably-dead) grant, not strand it cold"
        )

        # (2) AMBIGUOUS escalation during prime → DEFERRED (token KEPT).
        _seed("amb-user")
        _refresh_backoff_state(state, "amb-user", "srv-oauth").ambiguous_streak = (
            _AMBIGUOUS_ESCALATION_THRESHOLD - 1
        )
        with patch(
            "turnstone.core.mcp_oauth._refresh_and_persist",
            side_effect=_raiser(_RefreshFailureClass.AMBIGUOUS),
        ):
            amb = await get_user_access_token_classified(
                app_state=state,
                user_id="amb-user",
                server_name="srv-oauth",
                revoke_ambiguous_escalation=False,
            )
        assert amb.kind == "refresh_failed_transient"
        assert store.get_user_token("amb-user", "srv-oauth") is not None, (
            "prime must DEFER (not revoke) a sustained-ambiguous escalation"
        )

        # (3) Control: lazy dispatch (default) DOES escalate-revoke the same.
        _seed("amb-lazy")
        _refresh_backoff_state(state, "amb-lazy", "srv-oauth").ambiguous_streak = (
            _AMBIGUOUS_ESCALATION_THRESHOLD - 1
        )
        with patch(
            "turnstone.core.mcp_oauth._refresh_and_persist",
            side_effect=_raiser(_RefreshFailureClass.AMBIGUOUS),
        ):
            lazy = await get_user_access_token_classified(
                app_state=state,
                user_id="amb-lazy",
                server_name="srv-oauth",
            )
        assert lazy.kind == "refresh_failed"
        assert store.get_user_token("amb-lazy", "srv-oauth") is None, (
            "lazy dispatch must still escalate-revoke a sustained-ambiguous grant"
        )

    def test_prime_user_pools_skips_already_connected(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher, expires_in_seconds=3600)
        self._wire(mgr, storage, cipher)

        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            entry.session = MagicMock()  # already connected

        _run_on_loop(loop, _seed())

        primed: list[tuple[str, str]] = []

        async def _fake_prime(
            self_inner: MCPClientManager, key: tuple[str, str], cfg: dict[str, Any], token: str
        ) -> int:
            primed.append(key)
            return 0

        mgr._prime_user_server = _fake_prime.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        _run_on_loop(loop, mgr._prime_user_pools("user-1"))

        assert primed == [], "already-connected pool entry must be skipped"

    def test_schedule_prime_user_server_noop_for_non_oauth_user(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        self._wire(mgr, storage, cipher)
        mgr._oauth_user_server_names = set()  # nothing registered as oauth_user

        ran = threading.Event()

        async def _fake_logged(
            self_inner: MCPClientManager,
            key: tuple[str, str],
            cfg: dict[str, Any],
            token: str,
            user_id: str,
            server_name: str,
        ) -> None:
            ran.set()

        mgr._prime_user_server_logged = _fake_logged.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        mgr.schedule_prime_user_server(
            user_id="user-1", server_name="not-oauth", access_token="t", server_row={}
        )
        # Give any erroneously-scheduled coroutine a chance to run.
        _run_on_loop(loop, asyncio.sleep(0.05))
        assert not ran.is_set(), "non-oauth_user server must not schedule a prime"

    def test_schedule_prime_user_server_runs_for_oauth_user(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        self._wire(mgr, storage, cipher)

        captured: dict[str, Any] = {}
        done = threading.Event()

        async def _fake_prime(
            self_inner: MCPClientManager, key: tuple[str, str], cfg: dict[str, Any], token: str
        ) -> int:
            captured["key"] = key
            captured["token"] = token
            done.set()
            return 5

        mgr._prime_user_server = _fake_prime.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        server_row = storage.get_mcp_server_by_name("pool-srv")
        mgr.schedule_prime_user_server(
            user_id="user-1",
            server_name="pool-srv",
            access_token="bearer-x",
            server_row=server_row,
        )
        assert done.wait(timeout=5), "scheduled prime did not run on the mcp-loop"
        assert captured["key"] == ("user-1", "pool-srv")
        assert captured["token"] == "bearer-x"

    def test_dispatch_reconnects_when_bound_token_rotated(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """A warm session bound to a stale bearer is transparently reconnected
        with the current token; the discovered catalog is retained."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        # The CURRENT stored token the dispatch will resolve.
        _seed_user_token(storage, cipher, expires_in_seconds=3600, access_token="bearer-new")
        self._wire(mgr, storage, cipher)

        reconnect_tokens: list[str] = []

        async def _ok_call_tool(name: str, args: dict[str, Any]) -> Any:
            content = MagicMock()
            content.text = "ok"
            res = MagicMock()
            res.content = [content]
            res.isError = False
            return res

        async def _seed() -> None:
            entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
            sess = MagicMock()
            sess.call_tool = _ok_call_tool
            entry.session = sess
            entry.bound_token = "bearer-old"  # connected with the OLD token
            entry.tools = [{"name": "do_thing"}]  # catalog already discovered

        _run_on_loop(loop, _seed())

        async def _fake_connect(
            self_inner: MCPClientManager,
            key: tuple[str, str],
            cfg: dict[str, Any],
            access_token: str,
            *,
            auth_capture: Any = None,
            auth_fired_event: Any = None,
        ) -> Any:
            reconnect_tokens.append(access_token)
            entry = await self_inner._ensure_pool_entry(key)
            sess = MagicMock()
            sess.call_tool = _ok_call_tool
            entry.session = sess
            entry.bound_token = access_token
            return entry

        mgr._connect_one_pool = _fake_connect.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        result = mgr.call_tool_sync("mcp__pool-srv__do_thing", {}, user_id="user-1", timeout=5)

        assert result == "ok"
        # Stale bound token (bearer-old) != resolved token (bearer-new) -> exactly
        # one reconnect carrying the current bearer.
        assert reconnect_tokens == ["bearer-new"]
        # Catalog retained across the in-place rotation.
        entry = mgr._user_pool_entries[("user-1", "pool-srv")]
        assert entry.tools == [{"name": "do_thing"}]

    def test_prime_user_pools_skips_when_already_in_flight(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """A concurrent prime already in flight for (user, server) collapses the
        duplicate before the redundant DB reads."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher, expires_in_seconds=3600)
        self._wire(mgr, storage, cipher)
        mgr._priming_keys.add(("user-1", "pool-srv"))  # simulate an in-flight prime

        primed: list[tuple[str, str]] = []

        async def _fake_prime(
            self_inner: MCPClientManager, key: tuple[str, str], cfg: dict[str, Any], token: str
        ) -> int:
            primed.append(key)
            return 0

        mgr._prime_user_server = _fake_prime.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        _run_on_loop(loop, mgr._prime_user_pools("user-1"))

        assert primed == [], "an in-flight prime must collapse the duplicate"
        # The marker belongs to the other (still-running) prime — left intact.
        assert ("user-1", "pool-srv") in mgr._priming_keys

    def test_prime_user_pools_clears_in_flight_marker_after(
        self, running_loop_mgr, storage: SQLiteBackend
    ) -> None:
        """The in-flight marker is cleared in ``finally`` once a prime completes."""
        mgr, loop, _ = running_loop_mgr
        cipher = make_mcp_token_cipher()
        _seed_oauth_server(storage, name="pool-srv")
        _seed_user_token(storage, cipher, expires_in_seconds=3600)
        self._wire(mgr, storage, cipher)

        async def _fake_prime(
            self_inner: MCPClientManager, key: tuple[str, str], cfg: dict[str, Any], token: str
        ) -> int:
            return 1

        mgr._prime_user_server = _fake_prime.__get__(mgr, type(mgr))  # type: ignore[method-assign]

        _run_on_loop(loop, mgr._prime_user_pools("user-1"))

        assert mgr._priming_keys == set(), "in-flight marker must be cleared in finally"


class TestOAuthUserServerStatus:
    """``get_server_status`` for ``auth_type='oauth_user'`` servers reflects the
    REQUESTING user's pool warmth (scoped by user_id), never another user's — so
    the console pill flips to connected once that user's pool is primed, without
    leaking one user's catalog to another."""

    @staticmethod
    def _warm(mgr: MCPClientManager, user_id: str, server: str, n_tools: int = 1) -> None:
        from turnstone.core.mcp_client import PoolEntryState

        entry = PoolEntryState(key=(user_id, server), open_lock=MagicMock())
        entry.session = MagicMock()
        entry.tools = [{"function": {"name": f"mcp__{server}__t{i}"}} for i in range(n_tools)]
        mgr._user_pool_entries[(user_id, server)] = entry

    def test_oauth_user_status_connected_for_own_warm_pool(self) -> None:
        mgr = MCPClientManager({})
        mgr._oauth_user_server_names = {"pool-srv"}
        self._warm(mgr, "user-1", "pool-srv", n_tools=1)

        st = mgr.get_server_status("pool-srv", user_id="user-1")
        assert st["connected"] is True
        assert st["tools"] == 1
        assert st["auth_type"] == "oauth_user"
        assert st["user_pools"] == 1
        # Also surfaced in the all-servers map (oauth_user is absent from
        # _server_configs, so this exercises the explicit union).
        assert "pool-srv" in mgr.get_all_server_status(user_id="user-1")

    def test_oauth_user_status_does_not_leak_other_users_pool(self) -> None:
        """#4 regression: user B must NOT see user A's warm pool — neither the
        connected flag nor the catalog count. Before scoping, status was derived
        from warm[0] (an arbitrary user), leaking A's catalog size to B over the
        read-scoped /mcp-status endpoint."""
        mgr = MCPClientManager({})
        mgr._oauth_user_server_names = {"pool-srv"}
        self._warm(mgr, "user-A", "pool-srv", n_tools=5)

        own = mgr.get_server_status("pool-srv", user_id="user-A")
        assert own["connected"] is True
        assert own["tools"] == 5

        other = mgr.get_server_status("pool-srv", user_id="user-B")
        assert other["connected"] is False, "user B must not see user A's pool as connected"
        assert other["tools"] == 0, "user B must not see user A's catalog size"
        assert other["user_pools"] == 0

    def test_oauth_user_status_no_user_context_is_not_connected(self) -> None:
        """A request with no user context (user_id falsy — e.g. an operator
        refresh/reconnect) reports not-connected rather than an arbitrary
        user's pool."""
        mgr = MCPClientManager({})
        mgr._oauth_user_server_names = {"pool-srv"}
        self._warm(mgr, "user-A", "pool-srv", n_tools=3)

        for uid in (None, ""):
            st = mgr.get_server_status("pool-srv", user_id=uid)
            assert st["connected"] is False, f"user_id={uid!r} must not see a pool"
            assert st["tools"] == 0
            assert st["user_pools"] == 0
            assert st["auth_type"] == "oauth_user"

    def test_oauth_user_status_connecting_when_no_warm_pool(self) -> None:
        mgr = MCPClientManager({})
        mgr._oauth_user_server_names = {"pool-srv"}
        st = mgr.get_server_status("pool-srv", user_id="user-1")
        assert st["connected"] is False
        assert st["tools"] == 0
        assert st["user_pools"] == 0
        assert st["auth_type"] == "oauth_user"

    def test_oauth_user_status_aggregate_sees_any_user_pool(self) -> None:
        """Admin cluster-health view (aggregate=True, gated on admin.mcp at the
        endpoint): connected + a representative catalog reflect ANY user's warm
        pool, so the operator "in use by anyone" pill works — while a non-admin
        caller (aggregate=False) still sees only their own pool."""
        mgr = MCPClientManager({})
        mgr._oauth_user_server_names = {"pool-srv"}
        self._warm(mgr, "user-A", "pool-srv", n_tools=4)

        # Aggregate: a different (or absent) user still sees the server in use.
        agg = mgr.get_server_status("pool-srv", user_id="user-B", aggregate=True)
        assert agg["connected"] is True
        assert agg["tools"] == 4
        assert agg["user_pools"] == 1
        assert mgr.get_server_status("pool-srv", user_id=None, aggregate=True)["connected"] is True

        # Non-aggregate stays strictly per-user (no cross-user disclosure).
        assert mgr.get_server_status("pool-srv", user_id="user-B")["connected"] is False

    def test_public_server_status_uses_aggregate_for_operator_endpoints(self) -> None:
        """#1 regression: the approve-scoped operator refresh/reconnect endpoints
        (_public_server_status) must report a warm oauth_user server as connected
        via the aggregate view — not the per-user default (user_id=None), which
        would render every in-use oauth_user server disconnected/empty right after
        a successful refresh."""
        from turnstone.server import _public_server_status

        mgr = MCPClientManager({})
        mgr._oauth_user_server_names = {"pool-srv"}
        self._warm(mgr, "user-A", "pool-srv", n_tools=2)

        status = _public_server_status(mgr, "pool-srv")
        assert status["connected"] is True
        assert status["tools"] == 2


# Suppress unused-import warning for AsyncMock.
_ = AsyncMock
