"""End-to-end exercise of the oauth_obo feature on the OSS path (RFC 8693).

Parallel to ``entra_e2e.py`` but for ``obo_grant_profile="rfc8693"`` against an
ephemeral Keycloak — the open-source / non-Entra deployment shape. Fully
headless (password grant, no browser), so it runs unattended.

Drives the REAL Turnstone code: ``MCPTokenStore.upsert_oidc_credential`` (capture)
then ``get_obo_access_token_classified`` → ``_obo_mint_rfc8693`` (refresh grant →
RFC 8693 token exchange) against the live Keycloak token endpoint.

Checks E1–E7 mirror the Entra harness:
  E1 mint audience A → token, aud claim carries A, cache row refresh_token_ct NULL
  E2 second call → cache hit, ZERO extra Keycloak calls
  E3 audience B from the SAME captured credential → aud carries B
  E4 rotation write-back (KC rotates the RT on the refresh leg)
  E5 force_refresh → re-mint (Keycloak call count increments)
  E6 unconsented audience C → NOT token, credential SURVIVES
  E7 cache flush → re-mint

M1-M3 drive the MODEL-backend mint (``mint_obo_access_token``, #898/#955) on
the same captured credential — the path an ``auth_mode=rfc8693_obo`` model
alias takes, distinct from the classified MCP path above:
  M1 model mint audience A with the alias's exchange scopes → token carries A
     (the #955 fix: model definitions now carry per-row ``obo_scopes``, so
     the exchange leg requests the audience's scope exactly as MCP rows do)
  M2 warm re-mint serves the synthetic ``__model_obo__`` cache row —
     identity-keyed on the owning alias, audience + scopes in the row's
     own columns — with zero IdP calls
  M3 an entra-leg mode (``entra_obo``) on this rfc8693 deployment refuses
     BEFORE any IdP traffic, recording cause=grant_profile_mismatch — the
     mode/profile pairing that replaced the pre-#955 overload

Env (set by keycloak_e2e.sh):
  KC_TOKEN_ENDPOINT, KC_ISSUER, KC_CLIENT_ID, KC_CLIENT_SECRET,
  KC_USER, KC_PASSWORD, AUD_A, SCOPE_A, AUD_B, SCOPE_B, AUD_C
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from typing import Any

import httpx

from turnstone.core.mcp_crypto import (
    MCPTokenCipher,
    MCPTokenCipherConfig,
    MCPTokenStore,
)
from turnstone.core.mcp_oauth import (
    get_obo_access_token_classified,
    mint_obo_access_token,
    model_mint_refusal_cause,
    model_obo_cache_server,
    model_obo_cause_key,
)
from turnstone.core.oidc import OIDCConfig
from turnstone.core.storage._sqlite import SQLiteBackend

USER = "e2e-user"
RESULTS: list[tuple[str, str]] = []


def record(status: str, msg: str) -> None:
    RESULTS.append((status, msg))
    print(f"[{status:>8}] {msg}")


def redact(token: str | None) -> str:
    return f"{token[:8]}...({len(token)} chars)" if token else "<absent>"


def jwt_claims(token: str) -> dict[str, Any]:
    seg = token.split(".")[1]
    pad = "=" * (-len(seg) % 4)
    out: dict[str, Any] = json.loads(base64.urlsafe_b64decode(seg + pad))
    return out


def aud_carries(token: str, want: str) -> tuple[bool, str]:
    """KC puts the exchanged audience in the aud claim (str or list)."""
    aud = jwt_claims(token).get("aud", [])
    auds = aud if isinstance(aud, list) else [aud]
    return want in auds, str(aud)


class _CountingClient:
    def __init__(self, inner: httpx.AsyncClient) -> None:
        self._inner = inner
        self.posts = 0

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        self.posts += 1
        return await self._inner.post(*args, **kwargs)


def _password_login(cfg: dict[str, str]) -> str:
    """Headless direct-access grant → a real refresh token for the user."""
    resp = httpx.post(
        cfg["KC_TOKEN_ENDPOINT"],
        data={
            "grant_type": "password",
            "client_id": cfg["KC_CLIENT_ID"],
            "client_secret": cfg["KC_CLIENT_SECRET"],
            "username": cfg["KC_USER"],
            "password": cfg["KC_PASSWORD"],
            "scope": "openid",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return str(resp.json()["refresh_token"])


def _seed(storage: SQLiteBackend, name: str, audience: str, scopes: str | None) -> None:
    storage.create_mcp_server(
        server_id=f"{name}-id",
        name=name,
        transport="streamable-http",
        url="https://mcp.example.invalid/sse",
        auth_type="oauth_obo",
        oauth_audience=audience,
        oauth_scopes=scopes,
    )


async def _run(cfg: dict[str, str], refresh_token: str) -> None:
    issuer = cfg["KC_ISSUER"]
    db_path = os.path.join(tempfile.mkdtemp(prefix="obo-kc-e2e-"), "e2e.db")
    storage = SQLiteBackend(db_path)
    from cryptography.fernet import Fernet

    raw = base64.urlsafe_b64decode(Fernet.generate_key())
    store = MCPTokenStore(storage, MCPTokenCipher(MCPTokenCipherConfig(keys=(raw,))), node_id="e2e")
    oidc_config = OIDCConfig(
        enabled=True,
        issuer=issuer,
        client_id=cfg["KC_CLIENT_ID"],
        client_secret=cfg["KC_CLIENT_SECRET"],
        token_endpoint=cfg["KC_TOKEN_ENDPOINT"],
        obo_grant_profile="rfc8693",
        capture_user_credential=True,
    )

    store.upsert_oidc_credential(USER, issuer, refresh_token=refresh_token)
    cap = store.get_oidc_credential(USER, issuer)
    if cap and cap["refresh_token"] == refresh_token:
        record("VERIFIED", f"capture: credential persisted ({redact(refresh_token)})")
    else:
        record("FAILED", "capture: credential did not round-trip")
        return

    _seed(storage, "kc-a", cfg["AUD_A"], cfg.get("SCOPE_A"))
    _seed(storage, "kc-b", cfg["AUD_B"], cfg.get("SCOPE_B"))
    if cfg.get("AUD_C"):
        _seed(storage, "kc-c", cfg["AUD_C"], None)  # no audience scope → unconsented

    inner = httpx.AsyncClient(timeout=20.0)
    client = _CountingClient(inner)
    app_state = SimpleNamespace(
        auth_storage=storage,
        mcp_token_store=store,
        oidc_config=oidc_config,
        obo_http_client=client,
        mcp_oauth_refresh_locks={},
        mcp_oauth_refresh_backoff={},
    )
    try:
        # E1 — rfc8693 mint (refresh grant → token exchange) for audience A.
        r = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="kc-a"
        )
        if r.kind == "token" and r.token:
            ok, aud = aud_carries(r.token, cfg["AUD_A"])
            row = storage.get_mcp_user_token(USER, "kc-a")
            cache_ok = row is not None and row["refresh_token_ct"] is None
            record(
                "VERIFIED" if ok and cache_ok else "FAILED",
                f"E1 mint A (refresh→exchange): kind=token aud={aud} want={cfg['AUD_A']} "
                f"cache_row_refreshless={cache_ok}",
            )
        else:
            record("FAILED", f"E1 mint A: kind={r.kind} (expected token)")
            return

        # E2 — cache hit.
        posts_before = client.posts
        r2 = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="kc-a"
        )
        record(
            "VERIFIED" if r2.kind == "token" and client.posts == posts_before else "FAILED",
            f"E2 cache hit: kind={r2.kind} extra_kc_calls={client.posts - posts_before} (want 0)",
        )

        # E3 — audience B from the SAME credential.
        rb = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="kc-b"
        )
        if rb.kind == "token" and rb.token:
            ok_b, aud_b = aud_carries(rb.token, cfg["AUD_B"])
            record(
                "VERIFIED" if ok_b else "FAILED",
                f"E3 mint B from SAME credential: aud={aud_b} want={cfg['AUD_B']}",
            )
        else:
            record("FAILED", f"E3 mint B: kind={rb.kind}")

        # E4 — rotation write-back (KC rotates the RT on the refresh leg).
        cred_now = store.get_oidc_credential(USER, issuer)
        rotated = cred_now is not None and cred_now["refresh_token"] != refresh_token
        record(
            "VERIFIED" if cred_now is not None else "FAILED",
            f"E4 rotation write-back: persisted={redact(cred_now['refresh_token']) if cred_now else '<gone>'} "
            f"rotated_from_initial={rotated}",
        )

        # E5 — force_refresh re-mints.
        posts_before = client.posts
        rf = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="kc-a", force_refresh=True
        )
        record(
            "VERIFIED" if rf.kind == "token" and client.posts > posts_before else "FAILED",
            f"E5 force_refresh re-mint: kind={rf.kind} kc_calls={client.posts - posts_before} (want >=1)",
        )

        # E6 — unconsented audience: not a token, credential survives.
        if cfg.get("AUD_C"):
            rc = await get_obo_access_token_classified(
                app_state=app_state, user_id=USER, server_name="kc-c"
            )
            cred_after = store.get_oidc_credential(USER, issuer)
            record(
                "VERIFIED" if rc.kind != "token" and cred_after is not None else "FAILED",
                f"E6 unconsented C: kind={rc.kind} (not token) credential_survives={cred_after is not None}",
            )
        else:
            record("SKIPPED", "E6 unconsented C: AUD_C not set")

        # E7 — cache flush → re-mint.
        store.delete_user_token(USER, "kc-a")
        posts_before = client.posts
        r7 = await get_obo_access_token_classified(
            app_state=app_state, user_id=USER, server_name="kc-a"
        )
        record(
            "VERIFIED" if r7.kind == "token" and client.posts > posts_before else "FAILED",
            f"E7 flush→re-mint: kind={r7.kind} kc_calls={client.posts - posts_before} (want >=1)",
        )

        # M1-M3 — MODEL backend mint on the rfc8693 profile: same captured
        # credential and legs as E1-E7, but through mint_obo_access_token —
        # the path an auth_mode=rfc8693_obo alias takes, carrying the
        # per-alias exchange scopes MCP rows always had (#955). The mint's
        # cache and cause records are identity-keyed on the owning alias, so
        # the harness names one per mode-variant exactly as a deployment
        # would define separate rows.
        posts_before = client.posts
        m1 = await mint_obo_access_token(
            app_state=app_state,
            user_id=USER,
            alias="model-a",
            audience=cfg["AUD_A"],
            scopes=cfg.get("SCOPE_A", ""),
            grant_leg="rfc8693",
        )
        m1_kc_calls = client.posts - posts_before
        if m1:
            ok1, why1 = aud_carries(m1, cfg["AUD_A"])
            record(
                "VERIFIED" if ok1 and m1_kc_calls > 0 else "FAILED",
                f"M1 model mint (rfc8693_obo, scoped exchange): token={redact(m1)} "
                f"aud_ok={ok1} ({why1}) kc_calls={m1_kc_calls} (want >=1)",
            )
        else:
            record(
                "FAILED",
                f"M1 model mint (rfc8693_obo): no token (kc_calls={m1_kc_calls}) — "
                "the #955 scope wire-through should mint here",
            )

        # M2 — warm re-mint serves the synthetic __model_obo__ cache row —
        # identity-keyed on the owning alias, audience + scopes in the row's
        # own columns — with zero IdP calls, and the row is named so
        # deprovisioning can find it by prefix.
        posts_before = client.posts
        m2 = await mint_obo_access_token(
            app_state=app_state,
            user_id=USER,
            alias="model-a",
            audience=cfg["AUD_A"],
            scopes=cfg.get("SCOPE_A", ""),
            grant_leg="rfc8693",
        )
        cache_row = storage.get_mcp_user_token(USER, model_obo_cache_server("model-a"))
        if m1:
            record(
                "VERIFIED"
                if m2 and client.posts == posts_before and cache_row is not None
                else "FAILED",
                f"M2 model cache-hit: token={redact(m2)} kc_calls="
                f"{client.posts - posts_before} (want 0) synthetic_row="
                f"{'present' if cache_row is not None else 'MISSING'}",
            )
        else:
            record("FAILED", "M2 model cache-hit: blocked behind M1 — M1 failed, see above")

        # M3 — the mode/profile pairing refusal that replaced the pre-#955
        # overload: an entra-leg mode on this rfc8693 deployment must yield
        # None with ZERO IdP calls and record the grant_profile_mismatch
        # cause the session heartbeat reads (under its own alias — a
        # deployment defines the entra-mode variant as its own row).
        posts_before = client.posts
        m3 = await mint_obo_access_token(
            app_state=app_state,
            user_id=USER,
            alias="model-a-entra",
            audience=cfg["AUD_A"],
            grant_leg="entra",
        )
        m3_cause = model_mint_refusal_cause(
            "model_obo", model_obo_cause_key("model-a-entra", grant_leg="entra"), USER
        )
        record(
            "VERIFIED"
            if m3 is None and client.posts == posts_before and m3_cause == "grant_profile_mismatch"
            else "FAILED",
            f"M3 mode/profile mismatch refusal: token={redact(m3)} (want absent) "
            f"kc_calls={client.posts - posts_before} (want 0) cause={m3_cause!r}",
        )
    finally:
        await inner.aclose()


def main() -> int:
    required = [
        "KC_TOKEN_ENDPOINT",
        "KC_ISSUER",
        "KC_CLIENT_ID",
        "KC_CLIENT_SECRET",
        "KC_USER",
        "KC_PASSWORD",
        "AUD_A",
        "AUD_B",
    ]
    cfg = {k: os.environ[k] for k in os.environ if k.startswith(("KC_", "AUD_", "SCOPE_"))}
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(f"Missing env: {', '.join(missing)} — run via keycloak_e2e.sh")
        return 2

    print("Headless password login to Keycloak (the credential the feature captures)...")
    refresh_token = _password_login(cfg)

    asyncio.run(_run(cfg, refresh_token))

    print("\n=== summary ===")
    for status, msg in RESULTS:
        print(f"  {status:>8}  {msg}")
    return 0 if all(s in ("VERIFIED", "SKIPPED") for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
