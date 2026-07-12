"""Unit tests for the single-credential OBO mint engine (issue #551).

Covers ``get_obo_access_token_classified`` and its grant legs
(``_obo_mint_entra`` / ``_obo_mint_rfc8693``) in ``turnstone/core/mcp_oauth.py``.

Request-body assertions follow the spike-verified wire shapes in
BRIEFING.md ("Verified wire shapes") — every mock asserts the EXACT form
payload posted to the IdP token endpoint (body-inspecting, not
call-counting):

- entra: ONE refresh-token grant always carrying
  ``scope=<audience>/.default`` (per-server ``oauth_scopes`` is ignored
  on this leg — a bare scope list would drop the audience);
- rfc8693: a refresh grant (NO scope key) for a subject token, then a
  token-exchange grant with ``audience=<server oauth_audience>`` and the
  per-server scope only when configured.

Semantics pinned here:

- minted tokens cache in ``mcp_user_tokens`` with ``refresh_token_ct``
  NULL (cache, not custody);
- rotation write-back persists the newest IdP refresh token on the
  shared credential;
- a PERMANENT rejection (AADSTS65001-style ``invalid_grant``) drops ONLY
  the per-server cache row — the shared credential is NEVER auto-deleted,
  so one mis-granted server cannot lock the user out of the rest;
- transient failures keep everything and arm the per-(user, server)
  cooldown that short-circuits the next attempt without an IdP call;
- misconfiguration (unusable grant profile / missing audience) is
  loud-but-retryable: ``refresh_failed_transient`` with zero IdP
  round-trips and no exception.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tests.conftest import make_mcp_token_cipher
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.mcp_oauth import get_obo_access_token_classified
from turnstone.core.oidc import OIDCConfig
from turnstone.core.storage._sqlite import SQLiteBackend

USER = "user-1"
SERVER = "srv-obo"
SERVER_ID = "srv-obo-id"
ISSUER = "https://idp.test"
TOKEN_ENDPOINT = "https://idp.test/token"
AUDIENCE = "api://aud-a"

_ISO = "%Y-%m-%dT%H:%M:%S"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "test.db"))


def _make_oidc_config(**overrides: Any) -> OIDCConfig:
    """Real ``OIDCConfig`` for the OBO engine (``obo_grant_profile`` defaults
    to ``"entra"`` on the dataclass; tests override it explicitly)."""
    defaults: dict[str, Any] = {
        "enabled": True,
        "issuer": ISSUER,
        "client_id": "cid",
        "client_secret": "csecret",
        "token_endpoint": TOKEN_ENDPOINT,
    }
    defaults.update(overrides)
    return OIDCConfig(**defaults)


def _make_app_state(
    storage: SQLiteBackend,
    *,
    http_client: httpx.AsyncClient,
    oidc_config: OIDCConfig,
) -> SimpleNamespace:
    return SimpleNamespace(
        auth_storage=storage,
        mcp_token_store=MCPTokenStore(storage, make_mcp_token_cipher(), node_id="test"),
        oidc_config=oidc_config,
        obo_http_client=http_client,
        mcp_oauth_refresh_locks={},
    )


def _seed_obo_server(
    storage: SQLiteBackend,
    *,
    oauth_scopes: str | None = None,
    oauth_audience: str | None = AUDIENCE,
) -> None:
    storage.create_mcp_server(
        server_id=SERVER_ID,
        name=SERVER,
        transport="streamable-http",
        url="https://mcp.example.com/mcp",
        auth_type="oauth_obo",
        oauth_scopes=oauth_scopes,
        oauth_audience=oauth_audience,
    )


def _seed_credential(state: SimpleNamespace, *, refresh_token: str = "rt-1") -> None:
    state.mcp_token_store.upsert_oidc_credential(USER, ISSUER, refresh_token=refresh_token)


def _seed_cache_row(
    state: SimpleNamespace,
    *,
    expires_in_seconds: int,
    access_token: str = "cached-at",
    audience: str = AUDIENCE,
    created_seconds_ago: int = 0,
) -> None:
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).strftime(_ISO)
    state.mcp_token_store.create_user_token(
        USER,
        SERVER,
        access_token=access_token,
        refresh_token=None,
        expires_at=expires_at,
        scopes=None,
        as_issuer=ISSUER,
        audience=audience,
    )
    if created_seconds_ago:
        # Backdate the row via direct SQL: create_user_token stamps
        # created=now, but the under-lock force_refresh gate treats a row
        # whose ``created`` >= the caller's lock-request time as "another
        # caller just minted" and reuses it — a row seeded in the same second
        # as the call reads as exactly that. Tests exercising the RE-MINT
        # path need a row that is unambiguously from the past.
        import sqlalchemy as sa

        from turnstone.core.storage._schema import mcp_user_tokens

        backdated = (datetime.now(UTC) - timedelta(seconds=created_seconds_ago)).strftime(_ISO)
        storage = state.auth_storage
        with storage._engine.connect() as conn:
            conn.execute(
                sa.update(mcp_user_tokens)
                .where(
                    (mcp_user_tokens.c.user_id == USER) & (mcp_user_tokens.c.server_name == SERVER)
                )
                .values(created=backdated)
            )
            conn.commit()


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


def _mint(state: SimpleNamespace) -> Any:
    async def _run() -> Any:
        return await get_obo_access_token_classified(
            app_state=state, user_id=USER, server_name=SERVER
        )

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# entra leg — one RT redemption with scope=<audience>/.default
# ---------------------------------------------------------------------------


class TestEntraLeg:
    def test_happy_path_mints_with_default_scope_and_caches(self, storage: SQLiteBackend) -> None:
        """Case 1: one POST with the exact spike-verified Entra body; the mint
        caches as a refresh-less ``mcp_user_tokens`` row with ``as_issuer`` /
        ``audience`` populated and ``expires_at`` derived from ``expires_in``."""
        _seed_obo_server(storage)  # empty oauth_scopes → scope falls back to /.default
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "at-minted", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)
        before = datetime.now(UTC)

        result = _mint(state)

        assert result.kind == "token"
        assert result.token == "at-minted"
        # Exact wire shape (BRIEFING.md, Entra redemption) — full-dict equality
        # also proves no stray keys (no resource=, no audience=) rode along.
        assert client.post.call_count == 1
        call = client.post.call_args
        assert call.args == (TOKEN_ENDPOINT,)
        assert call.kwargs["data"] == {
            "grant_type": "refresh_token",
            "refresh_token": "rt-1",
            "client_id": "cid",
            "client_secret": "csecret",
            "scope": f"{AUDIENCE}/.default",
        }
        # Cache row: refresh-less (cache, not custody), issuer/audience stamped.
        raw = storage.get_mcp_user_token(USER, SERVER)
        assert raw is not None
        assert raw["refresh_token_ct"] is None
        assert raw["as_issuer"] == ISSUER
        assert raw["audience"] == AUDIENCE
        assert raw["expires_at"] is not None
        expires = datetime.strptime(raw["expires_at"], _ISO).replace(tzinfo=UTC)
        remaining = expires - before
        assert timedelta(seconds=3500) <= remaining <= timedelta(seconds=3601)
        plain = state.mcp_token_store.get_user_token(USER, SERVER)
        assert plain is not None
        assert plain["access_token"] == "at-minted"
        assert plain["refresh_token"] is None

    def test_entra_ignores_oauth_scopes_and_always_pins_audience_default(
        self, storage: SQLiteBackend
    ) -> None:
        """Entra's ``scope`` is its only audience carrier, so it ALWAYS sends
        ``<audience>/.default`` and ignores a per-server ``oauth_scopes`` (a bare
        scope list would drop the audience → wrong-audience bearer). Regression
        guard for the review's D finding."""
        _seed_obo_server(storage, oauth_scopes="custom.scope")
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "at-minted", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        result = _mint(state)

        assert result.kind == "token"
        assert client.post.call_count == 1
        call = client.post.call_args
        assert call.args == (TOKEN_ENDPOINT,)
        # audience-qualified .default — NOT the raw oauth_scopes value.
        assert call.kwargs["data"]["scope"] == "api://aud-a/.default"

    def test_entra_cache_row_records_effective_scope_not_configured_scope(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding: a server scoped under rfc8693 that survives a switch
        to obo_grant_profile=entra mints <audience>/.default (ignoring
        oauth_scopes). The cache row must record the EFFECTIVE scope actually
        minted ('' — .default), NOT the configured 'custom.scope' — otherwise
        _is_fresh_obo_cache_row would keep serving the broad .default bearer
        believing it is the narrow configured one, and a scope narrowing that
        can't apply under entra would look like it did."""
        _seed_obo_server(storage, oauth_scopes="custom.scope")
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        client.post.return_value = _mk_response(
            200, {"access_token": "at-broad-default", "expires_in": 3600}
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        result = _mint(state)
        assert result.kind == "token"
        # The row records the effective (empty) scope, not "custom.scope".
        row = storage.get_mcp_user_token(USER, SERVER)
        assert row is not None
        assert (row["scopes"] or "") == ""

        # A second dispatch serves the cache honestly (fresh, right effective
        # scope) with ZERO additional mints — no spurious re-mint from a
        # scope-mismatch the entra leg could never resolve.
        client.post.reset_mock()
        result2 = _mint(state)
        assert result2.kind == "token"
        assert result2.token == "at-broad-default"
        assert client.post.call_count == 0

    def test_rotated_refresh_token_written_back_to_credential(self, storage: SQLiteBackend) -> None:
        """Case 5: Entra usually rotates the RT on redemption — the newest
        value MUST be persisted to the shared credential (write-back rule)."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(
                200,
                {"access_token": "at-minted", "expires_in": 3600, "refresh_token": "rt-2"},
            )
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state, refresh_token="rt-1")

        result = _mint(state)

        assert result.kind == "token"
        cred = state.mcp_token_store.get_oidc_credential(USER, ISSUER)
        assert cred is not None
        assert cred["refresh_token"] == "rt-2"
        # The rotated RT stays on the credential — the cache row is refresh-less.
        raw = storage.get_mcp_user_token(USER, SERVER)
        assert raw is not None
        assert raw["refresh_token_ct"] is None


# ---------------------------------------------------------------------------
# rfc8693 leg — refresh grant for a subject token, then token exchange
# ---------------------------------------------------------------------------


class TestRfc8693Leg:
    def test_happy_path_two_posts_exchange_and_rotation_write_back(
        self, storage: SQLiteBackend
    ) -> None:
        """Case 3: exactly TWO POSTs with the spike-verified Keycloak shapes —
        a scope-less refresh grant, then a token exchange whose
        ``subject_token`` is the FIRST call's access token; the RT rotated by
        call 1 is persisted to the credential."""
        _seed_obo_server(storage)  # empty oauth_scopes → exchange carries NO scope key
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(
                    200,
                    {
                        "access_token": "subject-at",
                        "refresh_token": "rt-rotated",
                        "expires_in": 300,
                    },
                ),
                _mk_response(200, {"access_token": "exchanged-at", "expires_in": 600}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=_make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state, refresh_token="rt-1")

        result = _mint(state)

        assert result.kind == "token"
        assert result.token == "exchanged-at"
        assert client.post.call_count == 2
        first, second = client.post.call_args_list
        assert first.args == (TOKEN_ENDPOINT,)
        # Refresh leg: full-dict equality proves NO scope key is sent.
        assert first.kwargs["data"] == {
            "grant_type": "refresh_token",
            "refresh_token": "rt-1",
            "client_id": "cid",
            "client_secret": "csecret",
        }
        assert second.args == (TOKEN_ENDPOINT,)
        assert second.kwargs["data"] == {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": "cid",
            "client_secret": "csecret",
            "subject_token": "subject-at",
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": AUDIENCE,
        }
        # Rotation write-back from the FIRST leg persists on the credential.
        cred = state.mcp_token_store.get_oidc_credential(USER, ISSUER)
        assert cred is not None
        assert cred["refresh_token"] == "rt-rotated"
        # Cached mint is the EXCHANGED token, refresh-less.
        plain = state.mcp_token_store.get_user_token(USER, SERVER)
        assert plain is not None
        assert plain["access_token"] == "exchanged-at"
        assert plain["refresh_token"] is None

    def test_per_server_scopes_carried_on_exchange_call(self, storage: SQLiteBackend) -> None:
        """Case 3 (scoped): with ``oauth_scopes`` set, the SECOND call carries
        ``scope`` (Keycloak optional audience scopes must be explicit) while
        the refresh leg still sends none."""
        _seed_obo_server(storage, oauth_scopes="custom.scope")
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(200, {"access_token": "subject-at", "expires_in": 300}),
                _mk_response(200, {"access_token": "exchanged-at", "expires_in": 600}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=_make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)

        result = _mint(state)

        assert result.kind == "token"
        assert client.post.call_count == 2
        first, second = client.post.call_args_list
        assert first.kwargs["data"] == {
            "grant_type": "refresh_token",
            "refresh_token": "rt-1",
            "client_id": "cid",
            "client_secret": "csecret",
        }
        assert second.kwargs["data"] == {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": "cid",
            "client_secret": "csecret",
            "subject_token": "subject-at",
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": AUDIENCE,
            "scope": "custom.scope",
        }

    def test_subject_leg_missing_access_token_is_transient(self, storage: SQLiteBackend) -> None:
        """A 200 refresh-leg body without ``access_token`` aborts BEFORE the
        exchange call and classifies transient — a malformed IdP response must
        not delete anything."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mk_response(200, {"refresh_token": "rt-x"}))
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=_make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)

        result = _mint(state)

        assert result.kind == "refresh_failed_transient"
        assert client.post.call_count == 1  # never reached the exchange leg
        assert state.mcp_token_store.get_oidc_credential(USER, ISSUER) is not None

    def test_rotation_from_refresh_leg_survives_exchange_leg_failure(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding B (the lockout bug): on a rotating IdP the refresh leg
        consumes rt-1 and rotates to rt-rotated; if the exchange leg then fails,
        the rotated RT MUST already be persisted (not lost) — else the next mint
        for every obo server would redeem the consumed rt-1 and cascade-lock the
        user out. The exchange-response RT must NOT overwrite the credential."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(
                    200,
                    {
                        "access_token": "subject-at",
                        "refresh_token": "rt-rotated",
                        "expires_in": 300,
                    },
                ),
                # Exchange leg fails (e.g. one server's audience not yet granted).
                _mk_response(400, {"error": "invalid_grant", "error_description": "AADSTS500..."}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=_make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state, refresh_token="rt-1")

        result = _mint(state)

        assert client.post.call_count == 2  # refresh leg + failed exchange
        # The rotated RT from the refresh leg is persisted despite the failure.
        cred = state.mcp_token_store.get_oidc_credential(USER, ISSUER)
        assert cred is not None
        assert cred["refresh_token"] == "rt-rotated"
        # A 400 exchange with invalid_grant is a PERMANENT rejection for THIS
        # server; the credential survives so the user's other servers are fine.
        assert result.kind == "refresh_failed"

    def test_mint_without_expires_in_gets_bounded_expiry_not_cached_forever(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding: a mint response omitting the (RFC 8693-optional)
        expires_in must NOT cache expires_at=NULL — the freshness gate reads NULL
        as never-expiring (correct for opaque oauth_user tokens, wrong for a
        short-lived minted obo token), so it would be served indefinitely and
        defeat audience/scope-narrowing that relies on TTL turnover. A missing
        expiry falls back to a bounded default so the row re-mints soon."""
        from datetime import datetime as _dt

        from turnstone.core.mcp_oauth import _OBO_DEFAULT_TTL_SECONDS

        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        # Entra-shaped single-POST mint, but the IdP omits expires_in.
        client.post = AsyncMock(return_value=_mk_response(200, {"access_token": "at-no-exp"}))
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        result = _mint(state)

        assert result.kind == "token"
        row = storage.get_mcp_user_token(USER, SERVER)
        assert row is not None
        # Not NULL — a bounded expiry within the default TTL window was stamped.
        assert row["expires_at"] is not None
        parsed = _dt.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        remaining = (parsed - datetime.now(UTC)).total_seconds()
        assert 0 < remaining <= _OBO_DEFAULT_TTL_SECONDS + 5

    def test_exchange_response_refresh_token_never_overwrites_credential(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding (1960): an RFC 8693 exchange response MAY carry its own
        (audience-scoped) refresh_token; it must never be written to the shared
        issuer-wide credential."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                # Refresh leg does NOT rotate (no refresh_token).
                _mk_response(200, {"access_token": "subject-at", "expires_in": 300}),
                # Exchange leg returns an audience-scoped RT — must be ignored.
                _mk_response(
                    200,
                    {
                        "access_token": "exchanged-at",
                        "refresh_token": "audience-rt",
                        "expires_in": 600,
                    },
                ),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=_make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state, refresh_token="rt-1")

        result = _mint(state)

        assert result.kind == "token"
        cred = state.mcp_token_store.get_oidc_credential(USER, ISSUER)
        assert cred is not None
        assert cred["refresh_token"] == "rt-1"  # unchanged — NOT "audience-rt"


# ---------------------------------------------------------------------------
# Cache-row and credential lookup short-circuits
# ---------------------------------------------------------------------------


class TestCacheAndCredentialLookup:
    def test_fresh_cache_row_returns_token_with_zero_http_calls(
        self, storage: SQLiteBackend
    ) -> None:
        """Case 4: a fresh ``mcp_user_tokens`` row is served straight from the
        cache — no IdP round-trip."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)
        _seed_cache_row(state, expires_in_seconds=3600, access_token="cached-at")

        result = _mint(state)

        assert result.kind == "token"
        assert result.token == "cached-at"
        assert client.post.call_count == 0

    def test_credential_present_hint_skips_the_pre_lock_existence_read(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding: when a caller already established the captured
        credential exists (priming does one read for ALL of a user's obo
        servers), get_obo_access_token_classified must skip its per-server
        pre-lock existence re-read — otherwise session start re-reads the
        credential N+1 times. With credential_present=True only the authoritative
        under-lock read remains (one raw read); without the hint there are two
        (pre-lock existence + under-lock)."""
        from unittest.mock import patch

        _seed_obo_server(storage)

        def _run_with_hint(hint: bool | None) -> tuple[Any, int]:
            client = MagicMock(spec=httpx.AsyncClient)
            client.post = AsyncMock(
                return_value=_mk_response(200, {"access_token": "minted-at", "expires_in": 3600})
            )
            state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
            _seed_credential(state)
            reads = {"n": 0}
            real = storage.get_oidc_user_credential

            def _counting(user_id, issuer):
                reads["n"] += 1
                return real(user_id, issuer)

            async def _go() -> Any:
                with patch.object(storage, "get_oidc_user_credential", side_effect=_counting):
                    return await get_obo_access_token_classified(
                        app_state=state,
                        user_id=USER,
                        server_name=SERVER,
                        credential_present=hint,
                    )

            res = asyncio.run(_go())
            # Clear the cache row so the next run mints again (independent count).
            storage.delete_mcp_user_token(USER, SERVER)
            return res, reads["n"]

        result_hint, reads_hint = _run_with_hint(True)
        assert result_hint.kind == "token"
        assert reads_hint == 1  # pre-lock skipped; only the under-lock read

        result_none, reads_none = _run_with_hint(None)
        assert result_none.kind == "token"
        assert reads_none == 2  # pre-lock existence + under-lock

    def test_stale_audience_cache_row_is_not_served_and_remints(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding (audience guard): a cached token minted for a DIFFERENT
        audience than the server's current one must NOT be served — an operator's
        audience narrowing has to take effect immediately, not at token TTL. The
        stale row is ignored and a fresh mint (for the current audience) runs."""
        _seed_obo_server(storage)  # server oauth_audience = AUDIENCE (api://aud-a)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "reminted-at", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)
        # A fresh, refresh-less cache row — but for the OLD/broader audience.
        _seed_cache_row(
            state, expires_in_seconds=3600, access_token="old-aud-at", audience="api://old-broad"
        )

        result = _mint(state)

        # Not served from the stale-audience cache; a real mint happened.
        assert result.kind == "token"
        assert result.token == "reminted-at"
        assert client.post.call_count == 1
        # The cache row is now for the current audience.
        row = storage.get_mcp_user_token(USER, SERVER)
        assert row is not None and row["audience"] == AUDIENCE

    def test_stale_scopes_cache_row_is_not_served_and_remints(self, storage: SQLiteBackend) -> None:
        """Review finding: the read-side freshness gate is the AUTHORITATIVE
        enforcement of a scope narrowing (the admin cache purge is best-effort).
        Under rfc8693 a row minted with the OLD, wider scopes must NOT be served
        after the server's scopes are narrowed — even if the purge failed — so
        the privilege reduction takes effect on the next dispatch, not at TTL."""
        _seed_obo_server(storage, oauth_scopes="api.read")  # server's CURRENT scopes
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(200, {"access_token": "subject-at", "expires_in": 300}),
                _mk_response(200, {"access_token": "reminted-narrow", "expires_in": 3600}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=_make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)
        # Fresh, right-audience, refresh-less — but minted with the OLD wider scopes.
        state.mcp_token_store.create_user_token(
            USER,
            SERVER,
            access_token="wide-scope-at",
            refresh_token=None,
            expires_at=(datetime.now(UTC) + timedelta(seconds=3600)).strftime(_ISO),
            scopes="api.read api.write",  # wider than the server's current api.read
            as_issuer=ISSUER,
            audience=AUDIENCE,
        )

        result = _mint(state)

        # The wider-scope row is NOT served; a fresh mint for the current scopes runs.
        assert result.kind == "token"
        assert result.token == "reminted-narrow"
        assert client.post.call_count == 2
        row = storage.get_mcp_user_token(USER, SERVER)
        assert row is not None and (row["scopes"] or "") == "api.read"

    def test_missing_credential_returns_missing_with_zero_http_calls(
        self, storage: SQLiteBackend
    ) -> None:
        """Case 6: no captured credential → ``missing`` (the dispatcher's
        consent affordance is a re-login, not per-server consent); the IdP is
        never contacted."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        # Deliberately NO upsert_oidc_credential.

        result = _mint(state)

        assert result.kind == "missing"
        assert client.post.call_count == 0


# ---------------------------------------------------------------------------
# Failure handling — the load-bearing custody semantics
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_permanent_rejection_drops_cache_row_but_never_the_credential(
        self, storage: SQLiteBackend
    ) -> None:
        """Case 7 (load-bearing): a verified AADSTS65001-style
        ``invalid_grant`` classifies PERMANENT — the per-server cache row is
        deleted (re-consent UX for THAT server) but the shared credential
        survives, so one missing tenant grant can't lock the user out of
        every other OBO server."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(
                400,
                {
                    "error": "invalid_grant",
                    "error_description": (
                        "AADSTS65001: The user or administrator has not consented to use "
                        "the application."
                    ),
                },
            )
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)
        _seed_cache_row(state, expires_in_seconds=-1000, access_token="stale-at")

        result = _mint(state)

        assert result.kind == "refresh_failed"
        # Cache row GONE...
        assert storage.get_mcp_user_token(USER, SERVER) is None
        # ...but the credential STILL EXISTS — never auto-deleted here.
        assert state.mcp_token_store.get_oidc_credential(USER, ISSUER) is not None
        # The revoke is audited through the shared choke point.
        events = storage.list_audit_events(action="mcp_server.oauth.token_revoked")
        assert len(events) == 1
        assert events[0]["user_id"] == USER
        assert events[0]["resource_id"] == SERVER_ID
        detail = events[0]["detail"]
        detail = json.loads(detail) if isinstance(detail, str) else detail
        assert detail["reason"] == "obo_mint_rejected"

    def test_transient_503_keeps_credential_and_cooldown_short_circuits_second_call(
        self, storage: SQLiteBackend
    ) -> None:
        """Case 8: a 503 is transient — nothing is deleted — and the armed
        cooldown makes an immediate second attempt return transient with NO
        additional IdP call."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(503, {"error": "temporarily_unavailable"})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        async def _run() -> tuple[Any, Any]:
            first = await get_obo_access_token_classified(
                app_state=state, user_id=USER, server_name=SERVER
            )
            second = await get_obo_access_token_classified(
                app_state=state, user_id=USER, server_name=SERVER
            )
            return first, second

        first, second = asyncio.run(_run())

        assert first.kind == "refresh_failed_transient"
        assert second.kind == "refresh_failed_transient"
        # Cooldown short-circuit: the second call never reached the IdP.
        assert client.post.call_count == 1
        assert state.mcp_token_store.get_oidc_credential(USER, ISSUER) is not None

    def test_missing_access_token_in_200_body_is_transient(self, storage: SQLiteBackend) -> None:
        """Case 9: a 200 body without ``access_token`` is a malformed-IdP
        blip — transient, credential kept, nothing cached."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mk_response(200, {"expires_in": 3600}))
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        result = _mint(state)

        assert result.kind == "refresh_failed_transient"
        assert client.post.call_count == 1
        assert state.mcp_token_store.get_oidc_credential(USER, ISSUER) is not None
        assert storage.get_mcp_user_token(USER, SERVER) is None  # nothing was cached

    def test_oversized_error_body_on_client_error_is_ambiguous_not_transient(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding: the shared body-size guard raised with the DEFAULT
        (TRANSIENT) class before the non-200 was classified, so a permanent
        dead-grant whose error body exceeded the cap would loop 'please retry'
        forever and NEVER escalate (TRANSIENT doesn't advance the streak). An
        over-sized CLIENT-error body is now classified AMBIGUOUS by status, so it
        still advances the ambiguous streak and escalates to the honest re-login
        / admin remedy after the threshold."""
        from turnstone.core.mcp_oauth import _refresh_backoff_state

        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        # 400 with an error body over the 64KB cap.
        client.post = AsyncMock(
            return_value=_mk_response(400, {"error": "invalid_grant", "pad": "x" * (70 * 1024)})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        result = _mint(state)

        # Immediate result is still a kept-token transient (streak below the
        # escalation threshold), but the AMBIGUOUS class advanced the streak —
        # the TRANSIENT default would have left it at 0 and never escalated.
        assert result.kind == "refresh_failed_transient"
        assert _refresh_backoff_state(state, USER, SERVER).ambiguous_streak == 1

    def test_permanent_rejection_logs_idp_error_text(self, storage: SQLiteBackend, caplog) -> None:
        """Review finding (2316): the permanent-rejection path must log the IdP
        error body (the token_revoked audit row carries only a reason code), so
        an operator can tell a missing tenant grant from a dead credential."""
        import logging

        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(
                400,
                {"error": "invalid_grant", "error_description": "AADSTS65001: no consent"},
            )
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        with caplog.at_level(logging.WARNING, logger="turnstone.mcp"):
            result = _mint(state)

        assert result.kind == "refresh_failed"
        blob = " ".join(r.getMessage() + str(getattr(r, "__dict__", "")) for r in caplog.records)
        assert "obo_mint_rejected" in blob
        assert "AADSTS65001" in blob  # the actual IdP error text survives

    def test_permanent_rejection_no_cache_row_emits_no_revoke_audit(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding: a permanent mint rejection with NO cache row (the
        common missing-tenant-grant case — the user never had a token for this
        server) must NOT emit a token_revoked audit for a row that never
        existed. The cooldown is still armed as the terminal backstop (the
        credential survives), and — because the failure was PERMANENT — the
        second dispatch surfaces the honest permanent classification during the
        cooldown window (not a misleading retryable transient)."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(400, {"error": "invalid_grant", "error_description": "dead"})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)  # no cache row — the common missing-tenant-grant case

        async def _run() -> tuple[Any, Any]:
            first = await get_obo_access_token_classified(
                app_state=state, user_id=USER, server_name=SERVER
            )
            second = await get_obo_access_token_classified(
                app_state=state, user_id=USER, server_name=SERVER
            )
            return first, second

        first, second = asyncio.run(_run())

        assert first.kind == "refresh_failed"
        # Terminal: the cooldown short-circuits the second dispatch — and reports
        # the PERMANENT classification, not a retryable transient.
        assert second.kind == "refresh_failed"
        assert client.post.call_count == 1  # NOT re-minted
        # No row was ever deleted → no bogus revoke audit.
        events = storage.list_audit_events(action="mcp_server.oauth.token_revoked")
        assert len(events) == 0
        assert state.mcp_token_store.get_oidc_credential(USER, ISSUER) is not None

    def test_permanent_rejection_with_cache_row_audits_exactly_once(
        self, storage: SQLiteBackend
    ) -> None:
        """Companion: when a cache row DID exist, the permanent rejection deletes
        it and audits token_revoked exactly ONCE. A later doomed re-mint (past
        the cooldown) finds no row to delete and must NOT append a second audit
        row — the crux of the audit-spam finding."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(400, {"error": "invalid_grant", "error_description": "dead"})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)
        _seed_cache_row(state, expires_in_seconds=-1000, access_token="stale-at")  # forces a mint

        from turnstone.core.mcp_oauth import _clear_refresh_backoff

        async def _run() -> None:
            # First dispatch: deletes the (stale) cache row + audits once.
            await get_obo_access_token_classified(app_state=state, user_id=USER, server_name=SERVER)
            # Clear the cooldown so the second dispatch actually re-mints (the
            # weekend-of-scheduled-runs scenario), then dispatch again.
            _clear_refresh_backoff(state, USER, SERVER)
            await get_obo_access_token_classified(app_state=state, user_id=USER, server_name=SERVER)

        asyncio.run(_run())

        assert client.post.call_count == 2  # re-minted after the cooldown cleared
        # But only ONE revoke audit — the second doomed mint found no row.
        events = storage.list_audit_events(action="mcp_server.oauth.token_revoked")
        assert len(events) == 1

    def test_force_refresh_during_cooldown_falls_through_on_fresh_cache(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding (2063): the pre-lock cooldown short-circuit is gated on
        actually needing a mint. A force_refresh 401-retry with a still-fresh
        cache row must fall THROUGH the armed cooldown to the locked path (so it
        can re-mint / pick up a cluster-mate's token) rather than fail transient."""
        import time

        from turnstone.core.mcp_oauth import _refresh_backoff_state

        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "at-reminted", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)
        # Backdated so the under-lock reuse gate reads it as an OLD mint —
        # this test is about the cooldown fall-through re-minting, not the
        # same-second single-flight reuse (covered separately below).
        _seed_cache_row(
            state,
            expires_in_seconds=3600,
            access_token="stale-but-fresh-exp",
            created_seconds_ago=30,
        )
        # Arm the cooldown as if a prior mint just failed transiently.
        _refresh_backoff_state(state, USER, SERVER).last_failure_monotonic = time.monotonic()

        async def _run() -> Any:
            return await get_obo_access_token_classified(
                app_state=state, user_id=USER, server_name=SERVER, force_refresh=True
            )

        result = asyncio.run(_run())

        # Fell through the cooldown and re-minted (unconditional gate would have
        # returned refresh_failed_transient with zero IdP calls).
        assert result.kind == "token"
        assert result.token == "at-reminted"
        assert client.post.call_count == 1

    def test_force_refresh_reuses_concurrently_minted_token_without_reminting(
        self, storage: SQLiteBackend
    ) -> None:
        """Serialized force_refresh waiters must single-flight the re-mint: a
        waiter that acquires the lock AFTER a peer already re-minted reuses the
        peer's fresh token instead of running its own redundant IdP redemption.
        The reuse is decided by token IDENTITY (the under-lock row holds a
        DIFFERENT token than the rejected one this caller came in with), not by
        mint time — so a same-second concurrent mint is still reused."""
        from unittest.mock import patch

        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()  # any IdP call would be a gate failure
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        def _fresh_row(access_token: str) -> Any:
            return {
                "user_id": USER,
                "server_name": SERVER,
                "access_token": access_token,
                "refresh_token": None,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=3600)).strftime(_ISO),
                "scopes": None,
                "as_issuer": ISSUER,
                "audience": AUDIENCE,
                "created": datetime.now(UTC).strftime(_ISO),
                "last_refreshed": None,
            }

        # Pre-lock read returns the rejected token; the under-lock re-read returns
        # a DIFFERENT token (a concurrent waiter re-minted while we held-waited).
        reads = [_fresh_row("rejected-at"), _fresh_row("peer-reminted-at")]
        with patch.object(state.mcp_token_store, "get_user_token", side_effect=reads):

            async def _run() -> Any:
                return await get_obo_access_token_classified(
                    app_state=state, user_id=USER, server_name=SERVER, force_refresh=True
                )

            result = asyncio.run(_run())

        assert result.kind == "token"
        assert result.token == "peer-reminted-at"  # reused the peer's fresh token
        assert client.post.call_count == 0  # no redundant redemption

    def test_force_refresh_remints_when_cache_still_holds_rejected_token(
        self, storage: SQLiteBackend
    ) -> None:
        """The other half of the identity gate: when the under-lock row still
        holds the SAME token the caller came in with (no peer re-minted), a
        force_refresh must RE-MINT — never re-serve the just-rejected bearer.
        A mint-time gate at 1-second ``created`` granularity would wrongly
        re-serve a token minted in the same second as the retry."""
        from unittest.mock import patch

        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(
                200, {"access_token": "genuinely-reminted", "expires_in": 3600}
            )
        )
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        rejected = {
            "user_id": USER,
            "server_name": SERVER,
            "access_token": "rejected-at",
            "refresh_token": None,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=3600)).strftime(_ISO),
            "scopes": None,
            "as_issuer": ISSUER,
            "audience": AUDIENCE,
            "created": datetime.now(UTC).strftime(_ISO),
            "last_refreshed": None,
        }
        # Both the pre-lock and under-lock reads return the SAME (rejected) token.
        with patch.object(
            state.mcp_token_store, "get_user_token", side_effect=[rejected, rejected]
        ):

            async def _run() -> Any:
                return await get_obo_access_token_classified(
                    app_state=state, user_id=USER, server_name=SERVER, force_refresh=True
                )

            result = asyncio.run(_run())

        assert result.kind == "token"
        assert result.token == "genuinely-reminted"  # re-minted, NOT re-served
        assert client.post.call_count == 1

    def test_rotation_persist_failure_does_not_break_the_mint(self, storage: SQLiteBackend) -> None:
        """Review finding: a storage error inside the rotation-persist callback
        escaped the classified-result contract (only MCPOAuthRefreshFailed is
        caught around mint()) and broke the in-flight dispatch — and on a
        strict-rotation IdP the consumed RT stayed stored either way. The
        persist is best-effort: the mint still returns its token (this
        dispatch works); the stale credential surfaces on a LATER mint at
        worst, instead of a raw exception now."""
        from unittest.mock import patch

        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(
                    200,
                    {
                        "access_token": "subject-at",
                        "refresh_token": "rt-rotated",
                        "expires_in": 300,
                    },
                ),
                _mk_response(200, {"access_token": "exchanged-at", "expires_in": 600}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=_make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state, refresh_token="rt-1")

        with patch.object(
            state.mcp_token_store,
            "update_oidc_credential_after_redeem",
            side_effect=RuntimeError("transient db blip"),
        ):
            result = _mint(state)

        assert result.kind == "token"
        assert result.token == "exchanged-at"
        # The stored credential still holds the OLD RT — the failed persist is
        # logged, never raised.
        cred = state.mcp_token_store.get_oidc_credential(USER, ISSUER)
        assert cred is not None
        assert cred["refresh_token"] == "rt-1"

    def test_disabled_retryable_config_rediscovers_and_mints(self, storage: SQLiteBackend) -> None:
        """Review finding: OIDC discovery ran boot-once — a node that booted
        during a transient IdP outage kept enabled=False forever and every obo
        mint on it failed "transient" until an operator restart. The mint path
        now probes runtime re-discovery (cooldown-gated) before classifying
        obo_misconfigured, so the node self-heals."""
        import dataclasses as _dc
        from unittest.mock import patch

        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "minted-at", "expires_in": 3600})
        )
        boot_failed = _dc.replace(
            _make_oidc_config(), enabled=False, token_endpoint="", discovery_retryable=True
        )
        state = _make_app_state(storage, http_client=client, oidc_config=boot_failed)
        _seed_credential(state)
        healed = _make_oidc_config()  # enabled, token_endpoint populated

        async def _fake_discover(cfg: Any, *, client: Any = None) -> Any:
            return healed

        with patch("turnstone.core.oidc.discover_oidc", new=_fake_discover):
            result = _mint(state)

        assert result.kind == "token"
        assert result.token == "minted-at"
        assert state.oidc_config.enabled is True

    def test_credential_decrypt_failure_is_classified_not_raised(
        self, storage: SQLiteBackend
    ) -> None:
        """Review finding (2099): an undecryptable captured credential (key
        rotated away) must return kind='decrypt_failure', not let
        MCPTokenDecryptError escape the classified-result contract."""
        from unittest.mock import patch

        from turnstone.core.mcp_crypto import MCPTokenDecryptError

        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        with patch.object(
            state.mcp_token_store,
            "get_oidc_credential",
            side_effect=MCPTokenDecryptError("key unknown", key_fingerprints_attempted=("ab12",)),
        ):
            result = _mint(state)

        assert result.kind == "decrypt_failure"
        assert client.post.call_count == 0  # never reached the IdP


# ---------------------------------------------------------------------------
# Misconfiguration — loud, retryable, and never an exception
# ---------------------------------------------------------------------------


class TestMisconfiguration:
    @pytest.mark.parametrize("profile", ["", "id_jag"])
    def test_unusable_grant_profile_is_transient_with_zero_http_calls(
        self, storage: SQLiteBackend, profile: str
    ) -> None:
        """Case 10a: an empty or unknown ``obo_grant_profile`` is an
        operator-fixable misconfig — retryable classification, zero IdP
        calls, no exception (fixing config heals without re-consent)."""
        _seed_obo_server(storage)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=_make_oidc_config(obo_grant_profile=profile),
        )
        _seed_credential(state)  # credential present — config alone blocks the mint

        result = _mint(state)

        assert result.kind == "refresh_failed_transient"
        assert client.post.call_count == 0

    def test_missing_server_audience_is_transient_with_zero_http_calls(
        self, storage: SQLiteBackend
    ) -> None:
        """Case 10b: a server row without ``oauth_audience`` cannot be minted
        for — same retryable misconfig outcome, zero IdP calls."""
        _seed_obo_server(storage, oauth_audience=None)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        result = _mint(state)

        assert result.kind == "refresh_failed_transient"
        assert client.post.call_count == 0
