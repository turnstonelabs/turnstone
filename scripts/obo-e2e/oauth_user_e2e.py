"""End-to-end exercise of the oauth_user feature against an ephemeral Keycloak.

Parallel to ``keycloak_e2e.py`` (the ``oauth_obo`` leg) but for
``auth_type="oauth_user"``: per-user OAuth 2.1 + PKCE against the MCP
server's own authorization server. Fully headless — Keycloak's login and
consent forms are submitted by a cookie-carrying HTTP client standing in for
the browser — so it runs unattended.

Drives the REAL product entry points over the wire, nothing mocked: the
console's four OAuth routes mounted on a small Starlette app the way
``tests/test_mcp_oauth_handlers.py`` mounts them, with the HTTP client the
product installs (``initialize_mcp_oauth_state``) talking to a live realm.
``handle_mcp_oauth_authorize`` → headless login → ``handle_mcp_oauth_callback``
→ ``get_user_access_token_classified`` → ``handle_mcp_oauth_revoke_connection``.
The "MCP server" is a local stub that serves only RFC 9728 protected-resource
metadata; it never speaks MCP, because nothing under test does either.

Checks:
  D1 discovery: PRM read at the RFC 9728 path-specific location (origin never
     probed), the path-bearing realm issuer resolved in three probes, issuer
     persisted on the row
  C1 authorize URL: PKCE S256, RFC 8707 resource = the canonical row URL,
     the callback derived from redirect_base
  C2 consent: headless login → callback 302 to return_url; token row persisted,
     ciphertext only; aud carries the canonical resource
  C3 the stored token is live at Keycloak; the callback made exactly one
     request (the code exchange — issuer and metadata already cached)
  C4 cache hit: ZERO requests
  C5 callback replay refused (state is single-use), ZERO requests
  C6 refresh after a real clock expiry: one token POST, no re-discovery,
     refresh-token rotation written back, the new token live
  C7 force_refresh → one more token POST
  C8 connections list carries the row without secret material
  C9 revoke: 204, row gone, lookup → missing, RFC 7009 POST to the realm,
     and the refresh token is dead AT KEYCLOAK afterwards
  N1 a PRM document declaring the wrong resource is refused; the origin
     fallback was probed and 404'd; no authorization-server traffic
  N2 a token whose aud omits the resource is refused at the callback;
     nothing persisted
  D2 the origin-level PRM location is accepted as the fallback
  D3 dynamic client registration (registration_mode=dcr) followed by consent
  D4 the resolved issuer is persisted on a row whose authorization server the
     in-process metadata cache already holds

Env (set by oauth_user_e2e.sh):
  KC_ISSUER, KC_CLIENT_ID, KC_NOAUD_CLIENT_ID, KC_USER, KC_PASSWORD,
  MCP_STUB_PORT, MCP_STUB_ORIGIN_PORT, REDIRECT_BASE
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import sys
import tempfile
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route

from turnstone.core.auth import AuthResult
from turnstone.core.mcp_client import _validate_oauth_user_url
from turnstone.core.mcp_crypto import (
    MCPTokenCipher,
    MCPTokenCipherConfig,
    MCPTokenStore,
)
from turnstone.core.mcp_oauth import (
    canonical_resource,
    close_mcp_oauth_state,
    get_user_access_token_classified,
    handle_mcp_oauth_authorize,
    handle_mcp_oauth_callback,
    handle_mcp_oauth_list_connections,
    handle_mcp_oauth_revoke_connection,
    initialize_mcp_oauth_state,
)
from turnstone.core.oidc import OIDCConfig
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

USER = "e2e-user"
START = "/v1/api/mcp/oauth/start"
CALLBACK = "/v1/api/mcp/oauth/callback"
CONNECTIONS = "/v1/api/mcp/oauth/connections"
PRM = "/.well-known/oauth-protected-resource"
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
    """Keycloak writes aud as a string for one audience and a list for several."""
    aud = jwt_claims(token).get("aud", [])
    auds = aud if isinstance(aud, list) else [aud]
    return want in auds, str(aud)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


class _PRMStub:
    """A resource server that serves only RFC 9728 metadata, on a real loopback port.

    Discovery fetches over the network, so an in-process ASGI app would not
    do. Every request path is recorded so a check can assert WHICH well-known
    location discovery read: the origin-level document is a fallback the code
    also supports, and a stub that 404s the path-specific location would
    exercise that fallback without anyone noticing.
    """

    def __init__(self, port: int, documents: dict[str, dict[str, Any]]) -> None:
        self.origin = f"http://127.0.0.1:{port}"
        self.requests: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib casing)
                stub.requests.append(self.path)
                doc = documents.get(self.path)
                if doc is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(doc).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def drain(self) -> list[str]:
        seen, self.requests = self.requests, []
        return seen

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Wire:
    """Every request the product's OAuth client sends, via httpx's request hook."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.calls: list[tuple[str, str]] = []
        client.event_hooks["request"].append(self._seen)

    async def _seen(self, request: httpx.Request) -> None:
        self.calls.append((request.method, str(request.url)))

    def drain(self) -> list[tuple[str, str]]:
        calls, self.calls = self.calls, []
        return calls


class _Browser:
    """The user's browser, reduced to what Keycloak's login needs.

    Keycloak marks its cookies ``Secure``. A browser still sends them to a
    loopback origin over http, because loopback is a secure context, but
    ``http.cookiejar`` does not, so the jar is kept by hand. Forms are
    submitted by hand too: the login form, and — for a client registered
    with consent required — the consent form.
    """

    def __init__(self, http: httpx.AsyncClient, user: str, password: str) -> None:
        self._http = http
        self._jar: dict[str, str] = {}
        self._user = user
        self._password = password

    async def _fetch(
        self, method: str, url: str, data: dict[str, str] | None = None
    ) -> httpx.Response:
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self._jar.items())}
        resp = await self._http.request(method, url, data=data, headers=headers)
        for raw in resp.headers.get_list("set-cookie"):
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(raw)
            for name, morsel in cookie.items():
                self._jar[name] = morsel.value
        return resp

    async def consent(self, authorize_url: str, callback_url: str) -> str:
        """Follow the authorize URL through Keycloak; return the redirect back to the callback."""
        resp = await self._fetch("GET", authorize_url)
        for _ in range(6):
            location = str(resp.headers.get("location", ""))
            if resp.status_code in (302, 303) and location:
                if location.startswith(callback_url):
                    return location
                resp = await self._fetch("GET", location)
                continue
            form = re.search(r'<form[^>]*action="([^"]+)"', resp.text)
            if resp.status_code != 200 or form is None:
                raise RuntimeError(f"Keycloak answered HTTP {resp.status_code} without a form")
            action = urllib.parse.urljoin(str(resp.url), html.unescape(form.group(1)))
            fields = {
                name: html.unescape(value)
                for name, value in re.findall(
                    r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"',
                    resp.text,
                )
            }
            if 'name="username"' in resp.text:
                fields.update(username=self._user, password=self._password, credentialId="")
            elif 'name="accept"' in resp.text:
                fields["accept"] = "Yes"
            else:
                raise RuntimeError("Keycloak showed a form the harness does not know")
            resp = await self._fetch("POST", action, data=fields)
        raise RuntimeError("Keycloak never redirected back to the callback")


class _StampUser(BaseHTTPMiddleware):
    """The console's auth middleware, reduced to the one fact the handlers read."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id=USER,
            scopes=frozenset({"write"}),
            token_source="config",
            permissions=frozenset({"read", "write"}),
        )
        response: Response = await call_next(request)
        return response


def _console_app(storage: SQLiteBackend, store: MCPTokenStore, redirect_base: str) -> Starlette:
    """The console's OAuth surface: its routes, its handlers, its app-state contract."""
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/mcp/oauth/start", handle_mcp_oauth_authorize),
                    Route("/api/mcp/oauth/callback", handle_mcp_oauth_callback),
                    Route("/api/mcp/oauth/connections", handle_mcp_oauth_list_connections),
                    Route(
                        "/api/mcp/oauth/connections/{server_name}",
                        handle_mcp_oauth_revoke_connection,
                        methods=["DELETE"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_StampUser)],
    )
    app.state.auth_storage = storage
    app.state.mcp_token_store = store
    app.state.oidc_config = OIDCConfig(enabled=False, redirect_base=redirect_base)
    return app


def _seed(
    storage: SQLiteBackend,
    name: str,
    url: str,
    *,
    client_id: str | None,
    scopes: str | None = "openid",
    registration_mode: str | None = None,
) -> None:
    storage.create_mcp_server(
        server_id=f"{name}-id",
        name=name,
        transport="streamable-http",
        url=url,
        auth_type="oauth_user",
        oauth_client_id=client_id,
        oauth_scopes=scopes,
        oauth_registration_mode=registration_mode,
    )


async def _run(cfg: dict[str, str]) -> None:
    issuer = cfg["KC_ISSUER"]
    redirect_base = cfg["REDIRECT_BASE"]
    callback_url = redirect_base + CALLBACK
    # The harness's own view of the realm, so the product's wire is judged
    # against endpoints it did not discover for itself.
    realm = httpx.get(issuer + "/.well-known/openid-configuration", timeout=15.0).json()
    token_endpoint = str(realm["token_endpoint"])

    stub = _PRMStub(
        int(cfg["MCP_STUB_PORT"]),
        {
            f"{PRM}/mcp": {
                "resource": f"http://127.0.0.1:{cfg['MCP_STUB_PORT']}/mcp",
                "authorization_servers": [issuer],
            },
            # Declares a sibling resource: RFC 9728 §3.3 binds the document
            # to the identifier its URL was derived from, so this must be
            # refused (N1). The origin-level location is absent on purpose,
            # so the refusal cannot be rescued by the fallback.
            f"{PRM}/wrong": {
                "resource": f"http://127.0.0.1:{cfg['MCP_STUB_PORT']}/other",
                "authorization_servers": [issuer],
            },
        },
    )
    # A server from before the path-specific location existed: only the
    # origin-level document, declaring the bare origin (D2).
    legacy = _PRMStub(
        int(cfg["MCP_STUB_ORIGIN_PORT"]),
        {
            PRM: {
                "resource": f"http://127.0.0.1:{cfg['MCP_STUB_ORIGIN_PORT']}",
                "authorization_servers": [issuer],
            }
        },
    )
    resource = f"{stub.origin}/mcp"

    db_path = os.path.join(tempfile.mkdtemp(prefix="oauth-user-e2e-"), "e2e.db")
    storage = SQLiteBackend(db_path)
    storage.create_user(USER, USER, "E2E User", "hash")
    raw = base64.urlsafe_b64decode(Fernet.generate_key())
    store = MCPTokenStore(storage, MCPTokenCipher(MCPTokenCipherConfig(keys=(raw,))), node_id="e2e")
    app = _console_app(storage, store, redirect_base)
    await initialize_mcp_oauth_state(app.state)
    wire = _Wire(app.state.mcp_oauth_http_client)
    console = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=redirect_base)
    kc = httpx.AsyncClient(timeout=20.0)

    async def userinfo(token: str) -> str | None:
        resp = await kc.get(
            str(realm["userinfo_endpoint"]), headers={"Authorization": f"Bearer {token}"}
        )
        return str(resp.json().get("preferred_username")) if resp.status_code == 200 else None

    try:
        # The row is admitted the way the console admits it: the https gate
        # (loopback exempt) and strict canonicalization at the write boundary.
        _validate_oauth_user_url(resource)
        canonical = canonical_resource(resource, strict=True)
        _seed(storage, "kc-mcp", resource, client_id=cfg["KC_CLIENT_ID"])

        # D1 — discovery through the authorize handler.
        start = await console.get(START, params={"server": "kc-mcp", "return_url": "/done"})
        gets = [url for method, url in wire.drain() if method == "GET"]
        prm_hits = stub.drain()
        if start.status_code != 302:
            record("FAILED", f"D1 authorize: HTTP {start.status_code} {start.text[:200]}")
            return
        row = storage.get_mcp_server_by_name("kc-mcp") or {}
        want_document = issuer + "/.well-known/openid-configuration"
        as_probes = [url for url in gets if url.startswith(issuer.rsplit("/realms/", 1)[0])]
        d1_ok = (
            prm_hits == [f"{PRM}/mcp"]
            and gets[:1] == [f"{stub.origin}{PRM}/mcp"]
            and as_probes[-1:] == [want_document]
            and row.get("oauth_as_issuer_cached") == issuer
        )
        record(
            "VERIFIED" if d1_ok else "FAILED",
            f"D1 discovery: prm_locations={prm_hits} (want path-specific only) "
            f"as_probes={len(as_probes)} document={as_probes[-1] if as_probes else None} "
            f"issuer_cached={row.get('oauth_as_issuer_cached') == issuer}",
        )

        # C1 — the authorize URL the browser is sent to.
        authorize_url = start.headers["location"]
        authz = _query(authorize_url)
        c1_ok = (
            authorize_url.startswith(str(realm["authorization_endpoint"]) + "?")
            and authz.get("code_challenge_method") == "S256"
            and bool(authz.get("code_challenge"))
            and authz.get("resource") == canonical
            and authz.get("redirect_uri") == callback_url
            and authz.get("client_id") == cfg["KC_CLIENT_ID"]
            and bool(authz.get("state"))
        )
        record(
            "VERIFIED" if c1_ok else "FAILED",
            f"C1 authorize URL: endpoint_ok={authorize_url.startswith(str(realm['authorization_endpoint']))} "
            f"pkce={authz.get('code_challenge_method')} resource={authz.get('resource')} "
            f"redirect_uri={authz.get('redirect_uri')}",
        )

        # C2 — headless login, then the callback the browser would carry back.
        browser = _Browser(kc, cfg["KC_USER"], cfg["KC_PASSWORD"])
        callback_location = await browser.consent(authorize_url, callback_url)
        state_ok = _query(callback_location).get("state") == authz.get("state")
        done = await console.get(callback_location)
        exchange = wire.drain()
        ct_row = storage.get_mcp_user_token(USER, "kc-mcp")
        plain = store.get_user_token(USER, "kc-mcp")
        if done.status_code != 302 or plain is None or ct_row is None:
            record(
                "FAILED",
                f"C2 consent: HTTP {done.status_code} location={done.headers.get('location')} "
                f"row={'present' if ct_row else 'MISSING'} state_ok={state_ok}",
            )
            return
        access, refresh = plain["access_token"], plain["refresh_token"]
        encrypted = (
            access.encode("utf-8") not in ct_row["access_token_ct"]
            and refresh is not None
            and ct_row["refresh_token_ct"] is not None
            and refresh.encode("utf-8") not in ct_row["refresh_token_ct"]
        )
        aud_ok, aud = aud_carries(access, canonical)
        c2_ok = (
            done.headers.get("location") == "/done"
            and state_ok
            and encrypted
            and aud_ok
            and jwt_claims(access).get("azp") == cfg["KC_CLIENT_ID"]
            and plain["as_issuer"] == issuer
            and bool(plain["expires_at"])
        )
        record(
            "VERIFIED" if c2_ok else "FAILED",
            f"C2 consent: redirect={done.headers.get('location')} state_ok={state_ok} "
            f"row_encrypted={encrypted} aud={aud} want={canonical} "
            f"expires_at={plain['expires_at']} refresh={redact(refresh)}",
        )

        # C3 — the stored token is real, and the callback did only the exchange.
        who = await userinfo(access)
        c3_ok = who == cfg["KC_USER"] and exchange == [("POST", token_endpoint)]
        record(
            "VERIFIED" if c3_ok else "FAILED",
            f"C3 token live: userinfo={who!r} callback_wire={exchange} (want one POST to the token endpoint)",
        )

        # C4 — cache hit.
        r4 = await get_user_access_token_classified(
            app_state=app.state, user_id=USER, server_name="kc-mcp"
        )
        calls = wire.drain()
        record(
            "VERIFIED" if r4.kind == "token" and r4.token == access and not calls else "FAILED",
            f"C4 cache hit: kind={r4.kind} same_token={r4.token == access} requests={len(calls)} (want 0)",
        )

        # C5 — the callback state is single-use.
        replay = await console.get(callback_location)
        calls = wire.drain()
        untouched = store.get_user_token(USER, "kc-mcp") == plain
        record(
            "VERIFIED"
            if replay.status_code == 302
            and "session+expired" in replay.headers.get("location", "")
            and not calls
            and untouched
            else "FAILED",
            f"C5 callback replay: HTTP {replay.status_code} location={replay.headers.get('location')} "
            f"requests={len(calls)} (want 0) row_untouched={untouched}",
        )

        # C6 — refresh after the token really expires. The realm issues
        # short-lived tokens so the clock, not a forged row, crosses the
        # product's refresh-ahead window.
        from turnstone.core.mcp_oauth import _ACCESS_TOKEN_REFRESH_SKEW_SECONDS

        due = datetime.strptime(str(plain["expires_at"]), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        wait = (
            due - timedelta(seconds=_ACCESS_TOKEN_REFRESH_SKEW_SECONDS - 2) - datetime.now(UTC)
        ).total_seconds()
        print(f"  waiting {max(wait, 0):.0f}s for the access token to enter the refresh window...")
        await asyncio.sleep(max(wait, 0))
        r6 = await get_user_access_token_classified(
            app_state=app.state, user_id=USER, server_name="kc-mcp"
        )
        calls = wire.drain()
        plain6 = store.get_user_token(USER, "kc-mcp")
        rotated = plain6 is not None and plain6["refresh_token"] not in (None, refresh)
        who6 = await userinfo(r6.token) if r6.token else None
        record(
            "VERIFIED"
            if r6.kind == "token"
            and r6.token != access
            and calls == [("POST", token_endpoint)]
            and rotated
            and plain6 is not None
            and plain6["last_refreshed"]
            and who6 == cfg["KC_USER"]
            else "FAILED",
            f"C6 refresh after expiry: kind={r6.kind} new_token={r6.token != access} "
            f"wire={calls} (want one token POST, no re-discovery) rotated={rotated} "
            f"last_refreshed={plain6['last_refreshed'] if plain6 else None} userinfo={who6!r}",
        )

        # C7 — force_refresh re-refreshes a fresh token. A force_refresh in the
        # same wall-clock second as a completed refresh is handed that refresh's
        # token by design (second-precision last_refreshed is the contention
        # tiebreak), so the check first lets the second turn.
        await asyncio.sleep(1.05 - datetime.now(UTC).microsecond / 1_000_000)
        r7 = await get_user_access_token_classified(
            app_state=app.state, user_id=USER, server_name="kc-mcp", force_refresh=True
        )
        calls = wire.drain()
        record(
            "VERIFIED"
            if r7.kind == "token" and r7.token != r6.token and calls == [("POST", token_endpoint)]
            else "FAILED",
            f"C7 force_refresh: kind={r7.kind} new_token={r7.token != r6.token} wire={calls}",
        )

        # C8 — the settings-page projection.
        listing = await console.get(CONNECTIONS)
        rows = listing.json().get("connections", []) if listing.status_code == 200 else []
        leaked = [k for r in rows for k in r if "token" in k or k.endswith("_ct")]
        record(
            "VERIFIED"
            if listing.status_code == 200
            and [r["server_name"] for r in rows] == ["kc-mcp"]
            and not leaked
            and rows[0]["as_issuer"] == issuer
            else "FAILED",
            f"C8 connections list: HTTP {listing.status_code} servers={[r['server_name'] for r in rows]} "
            f"secret_fields={leaked} (want none)",
        )

        # C9 — revoke. The upstream RFC 7009 call is fire-and-forget by design,
        # so the harness waits on the task set before judging the wire.
        from turnstone.core.mcp_oauth import _revoke_upstream_tasks

        before = store.get_user_token(USER, "kc-mcp")
        rt_before = before["refresh_token"] if before else None
        revoked = await console.delete(f"{CONNECTIONS}/kc-mcp")
        await asyncio.gather(*_revoke_upstream_tasks)
        calls = wire.drain()
        gone = storage.get_mcp_user_token(USER, "kc-mcp") is None
        r9 = await get_user_access_token_classified(
            app_state=app.state, user_id=USER, server_name="kc-mcp"
        )
        dead = await kc.post(
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": rt_before or "",
                "client_id": cfg["KC_CLIENT_ID"],
            },
        )
        dead_error = dead.json().get("error") if dead.status_code != 200 else None
        record(
            "VERIFIED"
            if revoked.status_code == 204
            and gone
            and r9.kind == "missing"
            and calls == [("POST", str(realm["revocation_endpoint"]))]
            and dead_error == "invalid_grant"
            else "FAILED",
            f"C9 revoke: HTTP {revoked.status_code} row_gone={gone} lookup={r9.kind} "
            f"wire={calls} (want one POST to the revocation endpoint) "
            f"refresh_with_revoked_rt={dead.status_code} {dead_error!r} (want invalid_grant)",
        )

        # N1 — a document declaring another resource.
        _seed(storage, "kc-wrong", f"{stub.origin}/wrong", client_id=cfg["KC_CLIENT_ID"])
        wrong = await console.get(START, params={"server": "kc-wrong"})
        calls = wire.drain()
        prm_hits = stub.drain()
        error = wrong.json().get("error", "") if wrong.status_code == 502 else wrong.text[:200]
        wrong_row = storage.get_mcp_server_by_name("kc-wrong") or {}
        record(
            "VERIFIED"
            if wrong.status_code == 502
            and "does not match" in error
            and prm_hits == [f"{PRM}/wrong", PRM]
            and all(url.startswith(stub.origin) for _, url in calls)
            and not wrong_row.get("oauth_as_issuer_cached")
            else "FAILED",
            f"N1 wrong-resource PRM: HTTP {wrong.status_code} error={error!r} "
            f"prm_locations={prm_hits} (want path-specific, then the origin fallback) "
            f"as_traffic={[u for _, u in calls if not u.startswith(stub.origin)]} (want none)",
        )

        # N2 — a real token whose aud does not carry the resource.
        _seed(storage, "kc-noaud", resource, client_id=cfg["KC_NOAUD_CLIENT_ID"])
        start2 = await console.get(START, params={"server": "kc-noaud"})
        wire.drain()
        stub.drain()
        if start2.status_code != 302:
            record("FAILED", f"N2 authorize: HTTP {start2.status_code} {start2.text[:200]}")
        else:
            browser2 = _Browser(kc, cfg["KC_USER"], cfg["KC_PASSWORD"])
            done2 = await console.get(
                await browser2.consent(start2.headers["location"], callback_url)
            )
            calls = wire.drain()
            persisted = storage.get_mcp_user_token(USER, "kc-noaud") is not None
            record(
                "VERIFIED"
                if done2.status_code == 302
                and "audience+mismatch" in done2.headers.get("location", "")
                and not persisted
                and ("POST", token_endpoint) in calls
                else "FAILED",
                f"N2 audience mismatch: HTTP {done2.status_code} location={done2.headers.get('location')} "
                f"persisted={persisted} (want False) wire={calls}",
            )

        # D4 — the issuer is persisted on a row whose authorization server is
        # already in the in-process metadata cache. Discovery promises that
        # subsequent calls skip PRM; without the persisted issuer this row's
        # callback, every refresh and the revoke re-read the resource server's
        # metadata for as long as the cache entry lives.
        noaud_row = storage.get_mcp_server_by_name("kc-noaud") or {}
        record(
            "VERIFIED" if noaud_row.get("oauth_as_issuer_cached") == issuer else "FAILED",
            f"D4 issuer persisted with a warm metadata cache: "
            f"oauth_as_issuer_cached={noaud_row.get('oauth_as_issuer_cached')!r} want={issuer!r}",
        )

        # D2 — the origin-level fallback for a server that serves only there.
        _seed(storage, "kc-legacy", f"{legacy.origin}/legacy", client_id=cfg["KC_CLIENT_ID"])
        old = await console.get(START, params={"server": "kc-legacy"})
        calls = wire.drain()
        prm_hits = legacy.drain()
        record(
            "VERIFIED"
            if old.status_code == 302
            and prm_hits == [f"{PRM}/legacy", PRM]
            and all(url.startswith(legacy.origin) for _, url in calls)
            else "FAILED",
            f"D2 origin-level fallback: HTTP {old.status_code} prm_locations={prm_hits} "
            f"(want path-specific 404, then origin) "
            f"as_traffic={[u for _, u in calls if not u.startswith(legacy.origin)]} (want none: metadata cached)",
        )

        # D3 — dynamic client registration, then consent on the registered
        # client. No configured scopes: the realm's registration policy admits
        # only client scopes it defines, and the audience scope is a realm
        # default the new client inherits.
        _seed(storage, "kc-dcr", resource, client_id=None, scopes=None, registration_mode="dcr")
        start3 = await console.get(START, params={"server": "kc-dcr"})
        calls = wire.drain()
        dcr_row = storage.get_mcp_server_by_name("kc-dcr") or {}
        registered = str(dcr_row.get("oauth_client_id") or "")
        if start3.status_code != 302 or not registered:
            record(
                "FAILED",
                f"D3 dynamic client registration: HTTP {start3.status_code} "
                f"{start3.json().get('error') if start3.status_code != 302 else ''!r} "
                f"registration_wire={[(m, u) for m, u in calls if m == 'POST']}",
            )
        else:
            browser3 = _Browser(kc, cfg["KC_USER"], cfg["KC_PASSWORD"])
            done3 = await console.get(
                await browser3.consent(start3.headers["location"], callback_url)
            )
            plain3 = store.get_user_token(USER, "kc-dcr")
            aud3_ok, aud3 = (
                aud_carries(plain3["access_token"], canonical) if plain3 else (False, "<no row>")
            )
            record(
                "VERIFIED"
                if done3.status_code == 302
                and plain3 is not None
                and aud3_ok
                and jwt_claims(plain3["access_token"]).get("azp") == registered
                else "FAILED",
                f"D3 dynamic client registration: client_id={registered} "
                f"registration_wire={[(m, u) for m, u in calls if m == 'POST']} "
                f"consent={done3.headers.get('location')} aud={aud3}",
            )
    finally:
        await console.aclose()
        await kc.aclose()
        await close_mcp_oauth_state(app.state)
        stub.close()
        legacy.close()


def main() -> int:
    required = [
        "KC_ISSUER",
        "KC_CLIENT_ID",
        "KC_NOAUD_CLIENT_ID",
        "KC_USER",
        "KC_PASSWORD",
        "MCP_STUB_PORT",
        "MCP_STUB_ORIGIN_PORT",
        "REDIRECT_BASE",
    ]
    cfg = {k: os.environ.get(k, "") for k in required}
    missing = [k for k in required if not cfg[k]]
    if missing:
        print(f"Missing env: {', '.join(missing)} — run via oauth_user_e2e.sh")
        return 2

    asyncio.run(_run(cfg))

    print("\n=== summary ===")
    for status, msg in RESULTS:
        print(f"  {status:>8}  {msg}")
    return 0 if all(s in ("VERIFIED", "SKIPPED") for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
