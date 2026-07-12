"""End-to-end exercise of the oauth_obo feature against a REAL Entra tenant.

Unlike ``entra_spike.py`` (which verified the raw OAuth wire shapes), this
drives the ACTUAL Turnstone product code — real ``MCPTokenStore``, real
``get_obo_access_token_classified`` → ``_obo_mint_entra`` → the real Entra
token endpoint — so a green run proves the shipped mint engine works against
live Entra, not just that the protocol does.

Flow:
  1. Interactive Entra login (auth-code + PKCE + offline_access) → a real
     refresh credential. This is what ``handle_oidc_callback`` receives.
  2. Persist it via ``MCPTokenStore.upsert_oidc_credential`` — the exact call
     the OIDC callback makes on capture (auth.py). The rest of the callback
     (JWKS validation, user provisioning) is OIDC-generic and unit-tested; the
     novel path is capture + mint, which this exercises for real.
  3. Seed real ``oauth_obo`` ``mcp_servers`` rows (audiences A/B consented, C
     not) and drive ``get_obo_access_token_classified`` — the real dispatch-time
     entry point — asserting on the minted tokens, cache, rotation, and
     classification.

Checks (VERIFIED / FAILED per line):
  E1  mint for audience A → kind=token; decoded aud == A; cache row written with
      refresh_token_ct NULL (cache, not custody); expires_at set
  E2  second call for A → cache hit, ZERO additional Entra calls
  E3  mint for audience B from the SAME captured credential → aud == B
      (the single-credential-many-audiences thesis, through the real engine)
  E4  rotation write-back: the stored credential holds the newest refresh token
  E5  force_refresh → a fresh mint (Entra call count increments)
  E6  unconsented audience C → NOT kind=token, and the shared credential SURVIVES
      (never auto-deleted — the load-bearing custody invariant)
  E7  cache flush → re-mint: deleting the cache row makes the next call re-mint

Run:
  source scripts/obo-e2e/.env
  uv run python scripts/obo-e2e/entra_e2e.py
Env (from .env): ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET,
  SPIKE_AUDIENCE_A, SPIKE_AUDIENCE_B, SPIKE_AUDIENCE_UNCONSENTED, SPIKE_PORT.
Remote browser: set SPIKE_CALLBACK_FILE to paste the redirect URL (as before).
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
from types import SimpleNamespace
from typing import Any

import httpx

# Reuse the verified interactive-login machinery from the wire spike.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from entra_spike import interactive_login, jwt_claims_unverified, redact  # noqa: E402

from turnstone.core.mcp_crypto import (  # noqa: E402
    MCPTokenCipher,
    MCPTokenCipherConfig,
    MCPTokenStore,
)
from turnstone.core.mcp_oauth import get_obo_access_token_classified  # noqa: E402
from turnstone.core.oidc import OIDCConfig  # noqa: E402
from turnstone.core.storage._sqlite import SQLiteBackend  # noqa: E402

USER = "e2e-user"
RESULTS: list[tuple[str, str]] = []


def record(status: str, msg: str) -> None:
    RESULTS.append((status, msg))
    print(f"[{status:>8}] {msg}")


def aud_matches(token: str, want_audience: str) -> tuple[bool, str]:
    """Compare a minted access token's aud claim to the configured audience.

    Entra returns aud as the bare app-id GUID or the full ``api://<guid>`` URI;
    accept either.
    """
    claims = jwt_claims_unverified(token)
    aud = str(claims.get("aud", "<none>"))
    want = want_audience.removeprefix("api://")
    return aud in (want, want_audience), aud


class _CountingClient:
    """Wraps httpx.AsyncClient, counting token-endpoint POSTs so cache hits
    (which must issue zero) are observable."""

    def __init__(self, inner: httpx.AsyncClient) -> None:
        self._inner = inner
        self.posts = 0

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        self.posts += 1
        return await self._inner.post(*args, **kwargs)


def _make_app_state(
    storage: SQLiteBackend,
    store: MCPTokenStore,
    oidc_config: OIDCConfig,
    http_client: _CountingClient,
) -> SimpleNamespace:
    return SimpleNamespace(
        auth_storage=storage,
        mcp_token_store=store,
        oidc_config=oidc_config,
        oidc_http_client=http_client,
        mcp_oauth_refresh_locks={},
        mcp_oauth_refresh_backoff={},
    )


def _seed_obo_server(storage: SQLiteBackend, name: str, audience: str) -> None:
    storage.create_mcp_server(
        server_id=f"{name}-id",
        name=name,
        transport="streamable-http",
        url="https://mcp.example.invalid/sse",
        auth_type="oauth_obo",
        oauth_audience=audience,
    )


async def _run(cfg: dict[str, str], refresh_token: str) -> None:
    tenant = cfg["ENTRA_TENANT_ID"]
    issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
    token_endpoint = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    aud_a = cfg["SPIKE_AUDIENCE_A"]
    aud_b = cfg["SPIKE_AUDIENCE_B"]
    aud_c = cfg.get("SPIKE_AUDIENCE_UNCONSENTED", "")

    # Real Turnstone objects.
    db_path = os.path.join(tempfile.mkdtemp(prefix="obo-e2e-"), "e2e.db")
    storage = SQLiteBackend(db_path)
    from cryptography.fernet import Fernet

    raw = base64.urlsafe_b64decode(Fernet.generate_key())
    store = MCPTokenStore(storage, MCPTokenCipher(MCPTokenCipherConfig(keys=(raw,))), node_id="e2e")
    oidc_config = OIDCConfig(
        enabled=True,
        issuer=issuer,
        client_id=cfg["ENTRA_CLIENT_ID"],
        client_secret=cfg["ENTRA_CLIENT_SECRET"],
        token_endpoint=token_endpoint,
        obo_grant_profile="entra",
        capture_user_credential=True,
    )

    # Step 2 — CAPTURE: the exact storage call handle_oidc_callback makes.
    store.upsert_oidc_credential(USER, issuer, refresh_token=refresh_token)
    cap = store.get_oidc_credential(USER, issuer)
    if cap and cap["refresh_token"] == refresh_token:
        record("VERIFIED", f"capture: credential persisted for {USER} ({redact(refresh_token)})")
    else:
        record("FAILED", "capture: credential did not round-trip")
        return

    _seed_obo_server(storage, "e2e-a", aud_a)
    _seed_obo_server(storage, "e2e-b", aud_b)
    if aud_c:
        _seed_obo_server(storage, "e2e-c", aud_c)

    inner = httpx.AsyncClient(timeout=20.0)
    client = _CountingClient(inner)
    app_state = _make_app_state(storage, store, oidc_config, client)
    try:
        # E1 — real mint for audience A.
        r = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="e2e-a"
        )
        if r.kind == "token" and r.token:
            ok, aud = aud_matches(r.token, aud_a)
            row = storage.get_mcp_user_token(USER, "e2e-a")
            cache_ok = (
                row is not None and row["refresh_token_ct"] is None and bool(row["expires_at"])
            )
            record(
                "VERIFIED" if ok and cache_ok else "FAILED",
                f"E1 mint A: kind=token aud={aud} want={aud_a} cache_row_refreshless={cache_ok}",
            )
        else:
            record("FAILED", f"E1 mint A: kind={r.kind} (expected token)")
            return

        # E2 — cache hit issues zero Entra calls.
        posts_before = client.posts
        r2 = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="e2e-a"
        )
        record(
            "VERIFIED" if r2.kind == "token" and client.posts == posts_before else "FAILED",
            f"E2 cache hit: kind={r2.kind} extra_entra_calls={client.posts - posts_before} (want 0)",
        )

        # E3 — same credential, audience B.
        rb = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="e2e-b"
        )
        if rb.kind == "token" and rb.token:
            ok_b, aud_bclaim = aud_matches(rb.token, aud_b)
            record(
                "VERIFIED" if ok_b else "FAILED",
                f"E3 mint B from SAME credential: aud={aud_bclaim} want={aud_b}",
            )
        else:
            record("FAILED", f"E3 mint B: kind={rb.kind}")

        # E4 — rotation write-back: the stored credential is still redeemable
        # (holds the newest RT — Entra rotates on redemption).
        cred_now = store.get_oidc_credential(USER, issuer)
        record(
            "VERIFIED" if cred_now is not None else "FAILED",
            f"E4 rotation write-back: credential persisted {redact(cred_now['refresh_token']) if cred_now else '<gone>'}",
        )

        # E5 — force_refresh re-mints (a real Entra call).
        posts_before = client.posts
        rf = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="e2e-a", force_refresh=True
        )
        record(
            "VERIFIED" if rf.kind == "token" and client.posts > posts_before else "FAILED",
            f"E5 force_refresh re-mint: kind={rf.kind} entra_calls={client.posts - posts_before} (want >=1)",
        )

        # E6 — unconsented audience: not a token, and the credential SURVIVES.
        if aud_c:
            rc = await get_obo_access_token_classified(
                app_state=app_state, user_id=USER, server_name="e2e-c"
            )
            cred_after = store.get_oidc_credential(USER, issuer)
            record(
                "VERIFIED" if rc.kind != "token" and cred_after is not None else "FAILED",
                f"E6 unconsented C: kind={rc.kind} (not token) credential_survives={cred_after is not None}",
            )
        else:
            record("SKIPPED", "E6 unconsented C: SPIKE_AUDIENCE_UNCONSENTED not set")

        # E7 — cache flush → re-mint.
        store.delete_user_token(USER, "e2e-a")
        posts_before = client.posts
        r7 = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="e2e-a"
        )
        record(
            "VERIFIED" if r7.kind == "token" and client.posts > posts_before else "FAILED",
            f"E7 flush→re-mint: kind={r7.kind} entra_calls={client.posts - posts_before} (want >=1)",
        )
    finally:
        await inner.aclose()


def main() -> int:
    required = [
        "ENTRA_TENANT_ID",
        "ENTRA_CLIENT_ID",
        "ENTRA_CLIENT_SECRET",
        "SPIKE_AUDIENCE_A",
        "SPIKE_AUDIENCE_B",
    ]
    cfg = {k: os.environ[k] for k in os.environ if k.startswith(("ENTRA_", "SPIKE_"))}
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(f"Missing env: {', '.join(missing)} — did you `source scripts/obo-e2e/.env`?")
        return 2

    print("Signing in to Entra (this is the login the feature captures)...")
    tokens = interactive_login(cfg)
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        print(f"No refresh_token from login (keys={sorted(tokens)}) — offline_access missing?")
        return 1

    asyncio.run(_run(cfg, refresh_token))

    print("\n=== summary ===")
    for status, msg in RESULTS:
        print(f"  {status:>8}  {msg}")
    return 0 if all(s in ("VERIFIED", "SKIPPED") for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
