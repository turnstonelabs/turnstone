"""Refresh-grant tests for ``get_user_access_token``.

The refresh path is the hottest hot-path in OAuth-MCP: every dispatch
call funnels through it, and any bug — double-refresh, swallowed
``revoke``, lost ``refresh_token`` — manifests as either a thundering
herd against the AS or a stuck "consent required" loop.

The concurrency test is the protocol-correctness highlight: TWO
``asyncio.create_task(get_user_access_token(...))`` against an expired
token, and we assert that the AS sees exactly ONE refresh POST and
both coroutines return the same access_token.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tests.conftest import make_mcp_token_cipher
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.mcp_oauth import get_user_access_token
from turnstone.core.storage._sqlite import SQLiteBackend

# Generous CI ceiling — cancel/drain is sub-millisecond on a healthy loop.
_CANCEL_WAIT_S = 5.0


class _ObservableLockCm:
    """Class-based context manager whose ``__exit__`` is observable.

    Used by :class:`TestPgRefreshLock` cancellation tests instead of
    ``@contextlib.contextmanager`` so the test can distinguish:

      * an explicit ``cm.__exit__(None, None, None)`` call from the drain
        coroutine (records ``(None, None, None)`` in :attr:`exit_calls`)
      * ``GeneratorExit`` thrown by the cm's generator finalizer during
        garbage collection (records ``(GeneratorExit, ...)``)

    Class-based ``__exit__`` runs only when called explicitly — generator
    finalization doesn't go through it — so an empty ``exit_calls`` is
    proof the drain didn't run, not just that GC didn't fire.
    """

    def __init__(
        self,
        *,
        enter_started: threading.Event | None = None,
        enter_release: threading.Event | None = None,
        enter_raises: BaseException | None = None,
    ) -> None:
        self.enter_thread: int | None = None
        self.exit_thread: int | None = None
        self.exit_calls: list[tuple[Any, Any, Any]] = []
        self._enter_started = enter_started
        self._enter_release = enter_release
        self._enter_raises = enter_raises

    def __enter__(self) -> None:
        self.enter_thread = threading.get_ident()
        if self._enter_started is not None:
            self._enter_started.set()
        if self._enter_release is not None:
            self._enter_release.wait(timeout=_CANCEL_WAIT_S)
        if self._enter_raises is not None:
            raise self._enter_raises
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        self.exit_thread = threading.get_ident()
        self.exit_calls.append((exc_type, exc, tb))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app_state(storage: SQLiteBackend, *, http_client: httpx.AsyncClient) -> SimpleNamespace:
    cipher = make_mcp_token_cipher()
    state = SimpleNamespace(
        auth_storage=storage,
        mcp_token_store=MCPTokenStore(storage, cipher, node_id="test"),
        mcp_oauth_http_client=http_client,
        mcp_oauth_refresh_locks={},
        mcp_oauth_metadata_cache={},
    )
    return state


def _seed_server(backend: SQLiteBackend, *, server_id: str = "srv-id") -> None:
    backend.create_mcp_server(
        server_id=server_id,
        name="srv-oauth",
        transport="streamable-http",
        url="https://mcp.example.com/sse",
        auth_type="oauth_user",
        oauth_client_id="client-abc",
        oauth_scopes="openid profile",
        oauth_audience="https://mcp.example.com",
    )
    backend.update_mcp_server(server_id, oauth_as_issuer_cached="https://as.example.com")


def _seed_token(
    state: SimpleNamespace,
    *,
    user_id: str = "user-1",
    server_name: str = "srv-oauth",
    expires_in_seconds: int = 3600,
    refresh: str | None = "refresh-rrr",
) -> None:
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    state.mcp_token_store.create_user_token(
        user_id,
        server_name,
        access_token="access-aaa",
        refresh_token=refresh,
        expires_at=expires_at,
        scopes="openid profile",
        as_issuer="https://as.example.com",
        audience="https://mcp.example.com",
    )


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "test.db"))


def _good_as_metadata_doc() -> dict[str, Any]:
    return {
        "issuer": "https://as.example.com",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
        "jwks_uri": "https://as.example.com/jwks",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def _mk_response(status_code: int = 200, json_body: Any = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = {}
    body = "" if json_body is None else str(json_body)
    resp.content = body.encode("utf-8")
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    resp.text = body
    return resp


def _public_addr_patch():
    return patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestUnchangedToken:
    def test_returns_existing_token_when_not_expired(self, storage: SQLiteBackend) -> None:
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock()
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=3600)

        async def _run():
            return await get_user_access_token(
                app_state=state, user_id="user-1", server_name="srv-oauth"
            )

        token = asyncio.run(_run())
        assert token == "access-aaa"
        # No AS calls were made.
        client.get.assert_not_called()
        client.post.assert_not_called()

    def test_no_token_returns_none(self, storage: SQLiteBackend) -> None:
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        state = _make_app_state(storage, http_client=client)

        async def _run():
            return await get_user_access_token(
                app_state=state, user_id="user-1", server_name="srv-oauth"
            )

        assert asyncio.run(_run()) is None

    def test_get_user_access_token_handles_decrypt_error(self, storage: SQLiteBackend) -> None:
        """A decrypt failure on the stored token must not crash dispatch.

        When the operator rotates ``mcp_token_encryption_key`` and drops
        the prior key, every existing user-token row decrypts to
        :class:`MCPTokenDecryptError`. ``get_user_access_token`` MUST
        catch that and return ``None`` (forcing the user back through
        the consent flow) rather than propagating the exception up to
        the dispatch caller.
        """
        from turnstone.core.mcp_crypto import MCPTokenDecryptError

        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=3600)

        # Replace the get_user_token method on the store to raise the
        # canonical key-mismatch error.
        original_get = state.mcp_token_store.get_user_token

        def _raise_decrypt(*args, **kwargs):
            raise MCPTokenDecryptError(
                "no installed key can decrypt",
                key_fingerprints_attempted=("aabbccdd",),
            )

        state.mcp_token_store.get_user_token = _raise_decrypt

        try:

            async def _run():
                return await get_user_access_token(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

            result = asyncio.run(_run())
            assert result is None
        finally:
            state.mcp_token_store.get_user_token = original_get


# ---------------------------------------------------------------------------
# Refresh path
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_refreshes_when_expired(self, storage: SQLiteBackend) -> None:
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(
            return_value=_mk_response(
                200,
                {
                    "access_token": "access-NEW",
                    "refresh_token": "refresh-NEW",
                    "expires_in": 3600,
                },
            )
        )
        state = _make_app_state(storage, http_client=client)
        # Seed an expired token.
        _seed_token(state, expires_in_seconds=-1000)

        async def _run():
            with _public_addr_patch():
                return await get_user_access_token(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

        token = asyncio.run(_run())
        assert token == "access-NEW"
        # The refresh endpoint was hit exactly once.
        assert client.post.call_count == 1
        # Verify the new tokens were persisted.
        plain = state.mcp_token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None
        assert plain["access_token"] == "access-NEW"
        assert plain["refresh_token"] == "refresh-NEW"

    def test_refresh_failure_deletes_row_and_returns_none(self, storage: SQLiteBackend) -> None:
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        # AS rejects the refresh — token should be revoked.
        client.post = AsyncMock(
            return_value=_mk_response(
                400,
                {"error": "invalid_grant"},
            )
        )
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000)

        emitted: list[str] = []

        async def _run():
            from turnstone.core import mcp_oauth as mod

            real_record_audit = mod.record_audit

            def _capture(*args, **kwargs):
                # signature: (storage, user_id, action, resource_type, resource_id, detail)
                emitted.append(args[2] if len(args) >= 3 else kwargs.get("action", ""))
                return real_record_audit(*args, **kwargs)

            with (
                patch.object(mod, "record_audit", side_effect=_capture),
                _public_addr_patch(),
            ):
                return await get_user_access_token(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

        result = asyncio.run(_run())
        assert result is None
        # Row was deleted.
        assert state.mcp_token_store.get_user_token("user-1", "srv-oauth") is None
        # Audit emitted the revoke event.
        assert "mcp_server.oauth.token_revoked" in emitted

    def test_concurrent_callers_via_lock(self, storage: SQLiteBackend) -> None:
        """Two concurrent refresh calls must produce exactly one AS POST.

        Both callers see the same access_token.
        """
        _seed_server(storage)

        # Coordinate the AS POST so both callers race the lock.
        post_started = asyncio.Event()
        post_release = asyncio.Event()

        async def _post(url, *args, **kwargs):
            post_started.set()
            await post_release.wait()
            return _mk_response(
                200,
                {
                    "access_token": "access-ONE",
                    "refresh_token": "refresh-ONE",
                    "expires_in": 3600,
                },
            )

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(side_effect=_post)
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000)

        async def _both():
            with _public_addr_patch():
                t1 = asyncio.create_task(
                    get_user_access_token(
                        app_state=state, user_id="user-1", server_name="srv-oauth"
                    )
                )
                # Wait for the first task to enter the AS POST so the
                # second task is forced to take the lock contended.
                await post_started.wait()
                t2 = asyncio.create_task(
                    get_user_access_token(
                        app_state=state, user_id="user-1", server_name="srv-oauth"
                    )
                )
                # Give t2 a chance to queue on the lock.
                await asyncio.sleep(0.05)
                post_release.set()
                return await asyncio.gather(t1, t2)

        a, b = asyncio.run(_both())
        assert a == "access-ONE"
        assert b == "access-ONE"
        # Exactly one POST.
        assert client.post.call_count == 1

    def test_refresh_omitted_refresh_token_preserves_existing(self, storage: SQLiteBackend) -> None:
        """RFC 6749 §6 — AS MAY omit refresh_token; we PRESERVE the existing one.

        Production ASes (Google, default Auth0, default Okta) do NOT
        rotate refresh tokens. Clearing the column on every refresh
        would force the user to re-consent every hour. The contract:
        replace the persisted refresh token only when the AS issues a
        new one; otherwise pass the prior refresh token through to
        ``update_user_token_after_refresh``. The ``refresh_token=None``
        sentinel still means "clear" at the storage layer (Phase 3
        contract preserved); the *_refresh_and_persist_* layer
        translates "omitted" into "pass through existing".
        """
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(
            return_value=_mk_response(
                200,
                # No refresh_token in response.
                {"access_token": "access-NEW", "expires_in": 3600},
            )
        )
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000, refresh="refresh-original")

        async def _run():
            with _public_addr_patch():
                return await get_user_access_token(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

        token = asyncio.run(_run())
        assert token == "access-NEW"
        plain = state.mcp_token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None
        # Refresh column was PRESERVED — the original refresh token is
        # still usable for the next refresh cycle.
        assert plain["refresh_token"] == "refresh-original"

    def test_refresh_rotated_refresh_token_replaces_existing(self, storage: SQLiteBackend) -> None:
        """When AS issues a fresh refresh_token, the new value REPLACES the prior."""
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(
            return_value=_mk_response(
                200,
                {
                    "access_token": "access-NEW",
                    "refresh_token": "refresh-NEW",
                    "expires_in": 3600,
                },
            )
        )
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000, refresh="refresh-original")

        async def _run():
            with _public_addr_patch():
                return await get_user_access_token(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

        token = asyncio.run(_run())
        assert token == "access-NEW"
        plain = state.mcp_token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None
        assert plain["refresh_token"] == "refresh-NEW"

    def test_expired_no_refresh_token_revokes(self, storage: SQLiteBackend) -> None:
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000, refresh=None)

        async def _run():
            return await get_user_access_token(
                app_state=state, user_id="user-1", server_name="srv-oauth"
            )

        result = asyncio.run(_run())
        assert result is None
        # Row was deleted (re-consent path).
        assert state.mcp_token_store.get_user_token("user-1", "srv-oauth") is None

    def test_refresh_grant_sends_server_url_as_resource_not_audience(
        self, storage: SQLiteBackend
    ) -> None:
        """RFC 8707 ``resource=`` is the canonical MCP server URL.

        Earlier code passed ``oauth_audience`` as the resource value;
        Auth0-style ASes that honor a separate ``audience=`` parameter
        would then receive the wrong URL in ``resource=``, and ASes that
        validate ``resource`` against their RS allowlist would reject
        the refresh. The refresh-grant MUST send the canonical server
        URL on ``resource=``.
        """
        _seed_server(storage)
        # Override the audience on the seed server so it diverges from
        # the canonical server URL.
        backend_row = storage.get_mcp_server_by_name("srv-oauth")
        assert backend_row is not None
        storage.update_mcp_server(
            backend_row["server_id"],
            oauth_audience="https://different-audience.example.com/api",
        )
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(
            return_value=_mk_response(
                200,
                {
                    "access_token": "access-NEW",
                    "refresh_token": "refresh-NEW",
                    "expires_in": 3600,
                },
            )
        )
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000)

        async def _run():
            with _public_addr_patch():
                return await get_user_access_token(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

        asyncio.run(_run())
        # The refresh POST was made; assert the form payload sent
        # ``resource=server_url`` not ``resource=audience``.
        assert client.post.call_count == 1
        post_kwargs = client.post.call_args.kwargs
        sent_resource = post_kwargs["data"]["resource"]
        assert sent_resource == "https://mcp.example.com/sse"
        assert sent_resource != "https://different-audience.example.com/api"


# ---------------------------------------------------------------------------
# expires_in parsing — bug-2
# ---------------------------------------------------------------------------


class TestExpiresInParsing:
    """``_expires_at_from_response`` must accept int, float, and string.

    Real ASes have been seen returning ``3600.0`` (float) and ``"3600"``
    (string) — the prior ``int(str(3600.0))`` raised ValueError, leaving
    ``expires_at=None``. ``None`` then made ``_token_needs_refresh``
    return False, so the token was never refreshed and effectively never
    expired (it accumulated until the AS revoked it server-side).
    """

    def test_expires_in_float_parsed_correctly(self, storage: SQLiteBackend) -> None:
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(
            return_value=_mk_response(
                200,
                {
                    "access_token": "access-NEW",
                    "refresh_token": "refresh-NEW",
                    "expires_in": 3600.0,
                },
            )
        )
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000)

        async def _run():
            with _public_addr_patch():
                return await get_user_access_token(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

        token = asyncio.run(_run())
        assert token == "access-NEW"
        plain = state.mcp_token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None
        # expires_at must be populated — a None value here means the
        # float was rejected and the next refresh cycle would skip it.
        assert plain["expires_at"] is not None

    def test_expires_in_str_with_decimal_parsed_correctly(self, storage: SQLiteBackend) -> None:
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(
            return_value=_mk_response(
                200,
                {
                    "access_token": "access-NEW",
                    "refresh_token": "refresh-NEW",
                    "expires_in": "3600.0",
                },
            )
        )
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000)

        async def _run():
            with _public_addr_patch():
                return await get_user_access_token(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

        token = asyncio.run(_run())
        assert token == "access-NEW"
        plain = state.mcp_token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None
        assert plain["expires_at"] is not None

    def test_expires_in_int_string_parsed_correctly(self, storage: SQLiteBackend) -> None:
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(
            return_value=_mk_response(
                200,
                {
                    "access_token": "access-NEW",
                    "refresh_token": "refresh-NEW",
                    "expires_in": "3600",
                },
            )
        )
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000)

        async def _run():
            with _public_addr_patch():
                return await get_user_access_token(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

        token = asyncio.run(_run())
        assert token == "access-NEW"

    def test_expires_in_garbage_returns_none(self) -> None:
        from turnstone.core.mcp_oauth import _expires_at_from_response

        assert _expires_at_from_response({}) is None
        assert _expires_at_from_response({"expires_in": "abc"}) is None
        assert _expires_at_from_response({"expires_in": None}) is None
        assert _expires_at_from_response({"expires_in": True}) is None
        assert _expires_at_from_response({"expires_in": 0}) is None
        assert _expires_at_from_response({"expires_in": -5}) is None


# ---------------------------------------------------------------------------
# Multi-node refresh lock — advisory lock plumbing
# ---------------------------------------------------------------------------


class TestAdvisoryLock:
    """The refresh path must take the storage advisory lock as the OUTER
    serialization layer (cluster-wide), with the in-process asyncio.Lock
    as the inner layer. SQLite returns ``nullcontext`` so single-node
    deployments are untouched.
    """

    def test_advisory_lock_acquired_during_refresh(self, storage: SQLiteBackend) -> None:
        """The storage advisory lock must be acquired when refresh runs."""
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(
            return_value=_mk_response(
                200,
                {
                    "access_token": "access-NEW",
                    "refresh_token": "refresh-NEW",
                    "expires_in": 3600,
                },
            )
        )
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000)

        keys_seen: list[str] = []
        original = storage.acquire_advisory_lock_sync

        def _spy(key: str):  # type: ignore[no-untyped-def]
            keys_seen.append(key)
            return original(key)

        with patch.object(storage, "acquire_advisory_lock_sync", side_effect=_spy):

            async def _run():
                with _public_addr_patch():
                    return await get_user_access_token(
                        app_state=state, user_id="user-1", server_name="srv-oauth"
                    )

            asyncio.run(_run())

        assert keys_seen == ["mcp_refresh:user-1:srv-oauth"]

    def test_advisory_lock_is_noop_on_sqlite(self, storage: SQLiteBackend) -> None:
        """The SQLite backend's advisory lock is a ``nullcontext`` no-op."""
        import contextlib as _contextlib

        cm = storage.acquire_advisory_lock_sync("mcp_refresh:any:any")
        # ``nullcontext`` returns this exact object from __enter__.
        with cm as value:
            assert value is None
        assert isinstance(cm, _contextlib.nullcontext)


# ---------------------------------------------------------------------------
# _PgRefreshLock executor topology — independent of advisory-lock semantics
# ---------------------------------------------------------------------------


class TestPgRefreshLock:
    """Topology of the ``_PgRefreshLock`` async wrapper itself.

    These tests patch ``acquire_advisory_lock_sync`` with a fake context
    manager and assert behaviour of the per-instance executor, the
    cancellation-safety drain, and other lifecycle properties — not the
    advisory-lock semantics under it (those live in ``TestAdvisoryLock``).
    """

    def test_pg_refresh_lock_per_key_concurrency(self, storage: SQLiteBackend) -> None:
        """Two ``_PgRefreshLock`` instances with different keys must enter in parallel.

        Regression for the global single-worker executor shape: if every
        instance shared one ``ThreadPoolExecutor(max_workers=1)``, the
        second instance's ``__aenter__`` would queue behind the first
        even though the advisory keys are unrelated, so a single slow
        ``pg_try_advisory_xact_lock`` spin would block every other
        refresh on the node. Per-instance executors keep enter/exit on
        the same thread (psycopg2 thread-affinity) without globally
        serializing the spin loop.
        """
        from turnstone.core.mcp_oauth import _PgRefreshLock

        in_flight = 0
        max_in_flight = 0
        counter_lock = threading.Lock()
        enter_hold_s = 0.1

        @contextlib.contextmanager
        def _slow_lock_cm(_key_text: str = "") -> Any:
            nonlocal in_flight, max_in_flight
            with counter_lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            try:
                # Block INSIDE __enter__, before the yield, so concurrent
                # callers overlap on this stretch of the call.
                time.sleep(enter_hold_s)
                yield
            finally:
                with counter_lock:
                    in_flight -= 1

        with patch.object(storage, "acquire_advisory_lock_sync", side_effect=_slow_lock_cm):

            async def _hold(key: str) -> None:
                async with _PgRefreshLock(storage, key):
                    pass

            async def _two_concurrent() -> None:
                await asyncio.gather(_hold("key-a"), _hold("key-b"))

            asyncio.run(_two_concurrent())

        assert max_in_flight == 2, (
            "_PgRefreshLock instances serialized through a shared executor: "
            f"max_in_flight={max_in_flight}; expected 2 with per-instance executors."
        )

    def _run_cancel_scenario(
        self,
        storage: SQLiteBackend,
        *,
        enter_raises: BaseException | None = None,
    ) -> _ObservableLockCm:
        """Run the cancellation scenario shared by both cancellation tests.

        Patches ``acquire_advisory_lock_sync`` with a factory that returns
        a fresh :class:`_ObservableLockCm`, starts a ``_PgRefreshLock``
        acquire on a task, waits for the worker to enter ``cm.__enter__``,
        cancels the task, then releases the worker to either succeed
        (default) or raise (``enter_raises``). Awaits in-flight drain
        tasks via :data:`_pg_refresh_drain_tasks` so callers can inspect
        the cm deterministically.

        Test integrity:
        * Class-based cm — drain's explicit ``__exit__(None, None, None)``
          is recorded as a real method call, distinguishable from
          ``GeneratorExit`` thrown by GC of a generator-based cm.
        * Strong ref via ``created_cms`` — keeps the cm alive past the
          test's awaits, so a no-op drain genuinely fails the assertion
          rather than papering over via GC finalization timing.
        * Deterministic drain wait via :data:`_pg_refresh_drain_tasks` —
          no fixed-duration sleeps.

        Returns the single cm the factory created.
        """
        from turnstone.core.mcp_oauth import _pg_refresh_drain_tasks, _PgRefreshLock

        enter_started = threading.Event()
        enter_release = threading.Event()
        created_cms: list[_ObservableLockCm] = []

        def _factory(_key_text: str) -> _ObservableLockCm:
            cm = _ObservableLockCm(
                enter_started=enter_started,
                enter_release=enter_release,
                enter_raises=enter_raises,
            )
            created_cms.append(cm)
            return cm

        with patch.object(storage, "acquire_advisory_lock_sync", side_effect=_factory):

            async def _run() -> None:
                lock = _PgRefreshLock(storage, "key-cancel")

                async def _attempt() -> None:
                    async with lock:
                        pass  # body never runs — we cancel before it does

                task = asyncio.create_task(_attempt())
                # Wait until the worker is inside cm.__enter__.
                await asyncio.to_thread(enter_started.wait, _CANCEL_WAIT_S)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await task
                # Release the worker — succeed (default) or raise
                # (``enter_raises`` set via factory closure).
                enter_release.set()
                # Wait deterministically for any in-flight drain task.
                drains = list(_pg_refresh_drain_tasks)
                if drains:
                    await asyncio.gather(*drains, return_exceptions=True)

            asyncio.run(_run())

        assert len(created_cms) == 1, (
            f"factory was called {len(created_cms)} times — expected exactly 1"
        )
        return created_cms[0]

    def test_pg_refresh_lock_cancellation_releases_on_same_thread(
        self, storage: SQLiteBackend
    ) -> None:
        """Cancelling ``__aenter__`` mid-acquire MUST release on the same thread.

        Regression for: cancellation between submit and the worker
        completing ``cm.__enter__`` would otherwise leave an acquired
        Postgres advisory lock + open transaction whose paired
        ``cm.__exit__`` runs on a non-deterministic GC thread —
        violating psycopg2's connection thread-affinity (the very
        invariant the per-instance executor was introduced to enforce).
        The fire-and-forget drain coroutine waits for the worker to
        settle (via a fresh ``asyncio.wrap_future`` of the underlying
        ``concurrent.futures.Future``, NOT a re-await of the cancelled
        asyncio wrapper) and runs ``cm.__exit__`` on the same executor.
        """
        cm = self._run_cancel_scenario(storage)
        assert cm.exit_calls, (
            "drain did NOT call cm.__exit__ — orphan Postgres lock + open transaction"
        )
        # Drain calls __exit__(None, None, None); GC GeneratorExit would
        # have args (GeneratorExit, ..., ...). Distinguish.
        assert cm.exit_calls == [(None, None, None)], (
            f"cm.__exit__ called with {cm.exit_calls[0]} — expected "
            "(None, None, None) from drain. A non-(None,None,None) call would "
            "indicate the drain came in via GeneratorExit / GC instead of an "
            "explicit drain invocation."
        )
        assert cm.exit_thread == cm.enter_thread, (
            f"cm.__exit__ ran on thread {cm.exit_thread} but cm.__enter__ on "
            f"{cm.enter_thread} — psycopg2 thread-affinity violated"
        )

    def test_pg_refresh_lock_cancellation_no_lock_no_orphan(self, storage: SQLiteBackend) -> None:
        """If ``cm.__enter__`` raised, the drain must NOT call ``cm.__exit__``.

        The drain pairs only with successful acquires. If the worker
        raised (e.g., ``TimeoutError`` from the spin loop), there is no
        transaction to commit — calling ``__exit__`` on an unentered cm
        would itself raise.
        """
        cm = self._run_cancel_scenario(
            storage,
            enter_raises=TimeoutError("simulated spin-loop timeout"),
        )
        assert not cm.exit_calls, (
            "drain incorrectly called cm.__exit__ on an unentered cm "
            f"(calls: {cm.exit_calls}). Would attempt to commit a transaction "
            "that never began."
        )


# ---------------------------------------------------------------------------
# get_user_access_token_classified — tagged-result variant
# ---------------------------------------------------------------------------


class TestClassifiedGetter:
    """The classified getter distinguishes ``missing`` vs ``decrypt_failure``
    vs ``refresh_failed`` vs ``token`` so the dispatcher can map each to
    the right user-facing error.
    """

    def test_returns_token_on_happy_path(self, storage: SQLiteBackend) -> None:
        from turnstone.core.mcp_oauth import get_user_access_token_classified

        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=3600)

        async def _run():
            return await get_user_access_token_classified(
                app_state=state, user_id="user-1", server_name="srv-oauth"
            )

        result = asyncio.run(_run())
        assert result.kind == "token"
        assert result.token == "access-aaa"

    def test_missing_token_returns_missing(self, storage: SQLiteBackend) -> None:
        from turnstone.core.mcp_oauth import get_user_access_token_classified

        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        state = _make_app_state(storage, http_client=client)

        async def _run():
            return await get_user_access_token_classified(
                app_state=state, user_id="user-1", server_name="srv-oauth"
            )

        result = asyncio.run(_run())
        assert result.kind == "missing"

    def test_decrypt_failure_returns_decrypt_failure(self, storage: SQLiteBackend) -> None:
        """A key-mismatch must NOT collapse to ``missing``.

        RFC §5.3 — the dispatcher MUST distinguish "row missing" from
        "row present but undecryptable" so it can avoid emitting fake
        ``mcp_consent_required`` events on every operator misconfig.
        """
        from turnstone.core.mcp_crypto import MCPTokenDecryptError
        from turnstone.core.mcp_oauth import get_user_access_token_classified

        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=3600)

        def _raise_decrypt(*args, **kwargs):
            raise MCPTokenDecryptError(
                "no installed key can decrypt",
                key_fingerprints_attempted=("aabbccdd",),
            )

        state.mcp_token_store.get_user_token = _raise_decrypt

        async def _run():
            return await get_user_access_token_classified(
                app_state=state, user_id="user-1", server_name="srv-oauth"
            )

        result = asyncio.run(_run())
        assert result.kind == "decrypt_failure"
        assert result.decrypt_fingerprints == ("aabbccdd",)

    def test_refresh_failure_returns_refresh_failed(self, storage: SQLiteBackend) -> None:
        from turnstone.core.mcp_oauth import get_user_access_token_classified

        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        client.post = AsyncMock(return_value=_mk_response(400, {"error": "invalid_grant"}))
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000)

        async def _run():
            with _public_addr_patch():
                return await get_user_access_token_classified(
                    app_state=state, user_id="user-1", server_name="srv-oauth"
                )

        result = asyncio.run(_run())
        assert result.kind == "refresh_failed"
        # Row was deleted (re-consent path).
        assert state.mcp_token_store.get_user_token("user-1", "srv-oauth") is None

    def test_no_refresh_token_returns_refresh_failed(self, storage: SQLiteBackend) -> None:
        from turnstone.core.mcp_oauth import get_user_access_token_classified

        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=-1000, refresh=None)

        async def _run():
            return await get_user_access_token_classified(
                app_state=state, user_id="user-1", server_name="srv-oauth"
            )

        result = asyncio.run(_run())
        assert result.kind == "refresh_failed"

    def test_classified_does_not_break_legacy_helper(self, storage: SQLiteBackend) -> None:
        """The legacy ``get_user_access_token`` keeps its ``str | None`` contract."""
        _seed_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        state = _make_app_state(storage, http_client=client)
        _seed_token(state, expires_in_seconds=3600)

        async def _run():
            return await get_user_access_token(
                app_state=state, user_id="user-1", server_name="srv-oauth"
            )

        token = asyncio.run(_run())
        assert token == "access-aaa"
