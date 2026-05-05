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
