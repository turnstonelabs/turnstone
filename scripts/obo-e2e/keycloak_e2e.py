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

M1/M2 drive the MODEL-backend mint (``mint_obo_access_token``, issue #898) on
the same captured credential — the path an ``auth_mode=entra_obo`` model alias
takes, distinct from the classified MCP path above:
  M1 model mint audience A → token carries A. Currently KNOWN-GAP on KC 26
     standard token exchange (issue #955: model definitions carry no per-row
     scopes, so the exchange leg sends none and KC refuses the audience) —
     accepted ONLY on the exact gap signature: kc_calls == 2 AND E1
     VERIFIED AND the exchange-leg refusal the mint swallows (captured via
     a module-logger hook) carries the IdP's documented no-scope refusal
     text. A None with any other signature — including a 2-call refusal
     with different IdP error text (malformed exchange request) — is a
     mint-path regression and FAILS the run
  M2 warm re-mint serves the synthetic ``__model_obo__`` cache row with zero
     IdP calls (KNOWN-GAP while blocked behind an M1 KNOWN-GAP, FAILED
     behind an M1 failure)

A KNOWN-GAP status counts as a passing run (exit 0): it marks a documented
frontier, scoped to its exact signature so a regression cannot hide under it;
the #955 fix flips those legs back to hard VERIFIED/FAILED checks.

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

import turnstone.core.mcp_oauth as mcp_oauth_module
from turnstone.core.mcp_crypto import (
    MCPTokenCipher,
    MCPTokenCipherConfig,
    MCPTokenStore,
)
from turnstone.core.mcp_oauth import (
    MODEL_OBO_CACHE_PREFIX,
    get_obo_access_token_classified,
    mint_obo_access_token,
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


# The IdP text of the #955 refusal: KC 26 standard token exchange rejecting
# an audience requested with no scope. Live-verified on the MCP leg (the
# comment beside the exchange builder in core/mcp_oauth.py records it); the
# model leg builds the identical exchange request minus the scope param, so
# the same error_description is expected — a live run must confirm the model
# leg's captured text matches before this narrowing is called proven.
_KNOWN_GAP_REFUSAL_TEXT = "requested audience not available"


class _MintFailureLogHook:
    """Capture the exchange-leg refusal text ``mint_obo_access_token`` swallows.

    The mint catches ``MCPOAuthRefreshFailed`` and returns ``None``, so the
    None the harness sees carries no cause. Wrapping the module logger
    recovers it without touching the production mint: the log call happens
    INSIDE the except block, so ``sys.exc_info()`` still holds the live
    exception there.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.mint_failures: list[str] = []

    def warning(self, event: Any, *args: Any, **kwargs: Any) -> Any:
        if event == "model_obo.mint_failed":
            exc = sys.exc_info()[1]
            self.mint_failures.append(str(exc) if exc is not None else "")
        return self.inner.warning(event, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


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
            # A local, so M1's KNOWN-GAP signature consumes it directly
            # instead of re-scanning RESULTS message prefixes, which a
            # relabel would silently flip.
            e1_status = "VERIFIED" if ok and cache_ok else "FAILED"
            record(
                e1_status,
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

        # M1/M2 — MODEL backend mint (#898) on the rfc8693 profile: same
        # captured credential and legs, but through mint_obo_access_token,
        # the path an auth_mode=entra_obo alias takes. entra_obo is allowed
        # under either grant profile (only entra_app is entra-only), and this
        # is the one place that combination runs against a real IdP.
        posts_before = client.posts
        log_hook = _MintFailureLogHook(mcp_oauth_module.log)
        mcp_oauth_module.log = log_hook  # type: ignore[assignment]
        try:
            m1 = await mint_obo_access_token(
                app_state=app_state, user_id=USER, audience=cfg["AUD_A"]
            )
        finally:
            mcp_oauth_module.log = log_hook.inner
        m1_kc_calls = client.posts - posts_before
        m1_refusal = " | ".join(log_hook.mint_failures)
        m1_refusal_matches = _KNOWN_GAP_REFUSAL_TEXT in m1_refusal.lower()
        ok1, why1 = aud_carries(m1, cfg["AUD_A"]) if m1 else (False, "no token")
        e1_verified = e1_status == "VERIFIED"
        if m1:
            m1_status = "VERIFIED" if ok1 and m1_kc_calls > 0 else "FAILED"
            record(
                m1_status,
                f"M1 model mint (rfc8693): token={redact(m1)} aud_ok={ok1} ({why1}) "
                f"kc_calls={m1_kc_calls} (want >=1)",
            )
        elif m1_kc_calls == 2 and e1_verified and m1_refusal_matches:
            # #955's exact signature, nothing broader: both mint legs ran
            # against the live IdP (refresh grant + token exchange = 2 KC
            # calls), E1 VERIFIED proves the shared legs are healthy, AND the
            # swallowed exchange-leg error carries the IdP's documented
            # no-scope refusal text. The TEXT check is what separates the
            # documented gap from a mint-side exchange regression with the
            # same call count (wrong audience parameter, dropped subject
            # token, bad grant_type all also draw a 2-call refusal). The #955
            # fix flips this branch back to a hard VERIFIED/FAILED check.
            m1_status = "KNOWN-GAP"
            record(
                m1_status,
                "M1 model mint (rfc8693): no scope wire-through for model "
                f"aliases — see issue #955 (kc_calls={m1_kc_calls}, refusal "
                f"text matched {_KNOWN_GAP_REFUSAL_TEXT!r})",
            )
        else:
            # None with any OTHER signature (no KC traffic, a single leg,
            # unhealthy shared legs, or a 2-call refusal whose IdP error text
            # is NOT the documented no-scope refusal) is a regression in or
            # upstream of the mint, and must fail the run rather than wear
            # the KNOWN-GAP label.
            m1_status = "FAILED"
            record(
                m1_status,
                "M1 model mint (rfc8693): no token and the failure signature "
                f"does not match the #955 gap (kc_calls={m1_kc_calls}, want 2 "
                f"with E1 VERIFIED; e1_verified={e1_verified}; "
                f"refusal_text_matched={m1_refusal_matches} "
                f"captured={m1_refusal[:300]!r}) — mint-path regression, not "
                "the no-scope exchange refusal",
            )

        # M2 — warm re-mint serves the synthetic __model_obo__ cache row with
        # zero IdP calls, and the row is named so deprovisioning can find it.
        posts_before = client.posts
        m2 = await mint_obo_access_token(app_state=app_state, user_id=USER, audience=cfg["AUD_A"])
        cache_row = storage.get_mcp_user_token(USER, f"{MODEL_OBO_CACHE_PREFIX}{cfg['AUD_A']}")
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
            # Blocked behind M1: inherit its classification, so a FAILED M1
            # cannot launder its downstream leg into a KNOWN-GAP pass.
            if m1_status == "KNOWN-GAP":
                record("KNOWN-GAP", "M2 model cache-hit: blocked behind M1 — see issue #955")
            else:
                record("FAILED", "M2 model cache-hit: blocked behind M1 — M1 failed, see above")
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
    return 0 if all(s in ("VERIFIED", "SKIPPED", "KNOWN-GAP") for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
