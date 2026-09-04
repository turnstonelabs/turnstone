# OBO e2e harnesses — single-credential MCP token minting (`auth_type=oauth_obo`)

Manual test harnesses for the `oauth_obo` feature (issue #551). They exercise
the **real** Turnstone mint path (`get_obo_access_token_classified` →
`_obo_mint_entra` / `_obo_mint_rfc8693`) against a real identity provider — not
mocks, not the unit suite. Two grant legs:

- **Entra** (`entra_e2e.py`) — real tenant, one interactive sign-in.
- **Keycloak / RFC 8693** (`keycloak_e2e.py` + `.sh`) — ephemeral docker, fully
  headless.

The sibling per-user flow (`auth_type=oauth_user`, OAuth 2.1 + PKCE against the
MCP server's own authorization server) has its own harness in the same idiom:

- **Keycloak / per-user consent** (`oauth_user_e2e.py` + `.sh`) — ephemeral
  docker, fully headless (the harness submits Keycloak's login form itself).

There is also `entra_spike.py` (raw-OAuth **wire** probe, pre-implementation
reference) and `entra_setup.sh` (creates the Entra app registrations + writes a
populated `.env`).

**Secrets:** these read config from env. Real credentials live in a **gitignored
`.env`** (copy `.env.example`); nothing tenant-specific is committed. The only
literal secret in the tree is the ephemeral Keycloak container's throwaway
`spike-secret`, which lives and dies with the container.

Not part of CI — run by hand when validating the feature against a live IdP.

## `entra_e2e.py` — end-to-end product exercise (post-implementation)

`entra_spike.py` verified the raw OAuth WIRE (before code existed). `entra_e2e.py`
verifies the SHIPPED Turnstone code: it does a real Entra login, feeds the
credential through the real `MCPTokenStore.upsert_oidc_credential` (the call the
OIDC callback makes on capture), then drives the real
`get_obo_access_token_classified` → `_obo_mint_entra` against the live Entra token
endpoint. Checks E1–E7: real mint + aud claim, cache-hit (0 Entra calls),
single-credential→audiences A&B, rotation write-back, force_refresh re-mint,
unconsented-audience classification with the credential surviving, and
flush→re-mint. Reuses the same `.env` and interactive login (SPIKE_CALLBACK_FILE
for remote browser).

```bash
source scripts/obo-e2e/.env
uv run python scripts/obo-e2e/entra_e2e.py
# one interactive sign-in; E1–E7 then run against the real product code. Results below.
```

Results — RUN 2026-07-12 on the real tenant, ALL VERIFIED (exit 0): capture
persisted; E1 mint A (aud=A app-id, cache row refresh_token_ct NULL); E2 cache
hit (0 extra Entra calls); E3 mint B from the SAME credential (aud=B app-id); E4
rotation write-back (RT rotated 2040→2091 chars, newest persisted); E5
force_refresh re-mint (1 Entra call); E6 unconsented C → refresh_failed and the
credential SURVIVES; E7 flush→re-mint. The real `get_obo_access_token_classified`
→ `_obo_mint_entra` path against the live Entra token endpoint.

## `keycloak_e2e.py` + `keycloak_e2e.sh` — OSS path (RFC 8693), headless

The rfc8693 equivalent of `entra_e2e.py`: `keycloak_e2e.sh` spins up ephemeral
Keycloak, configures the realm (turnstone client with standard token exchange,
mcp-a/b/c clients, aud-mcp-a/b audience scopes, a test user), runs the harness
against the real `get_obo_access_token_classified` → `_obo_mint_rfc8693`
(refresh grant → token exchange), then tears down. No browser (password grant).

```bash
./scripts/obo-e2e/keycloak_e2e.sh
```

Results — RUN 2026-07-12, ALL VERIFIED: capture persisted; E1 mint A
(refresh→exchange, aud=mcp-a, cache row refresh_token_ct NULL); E2 cache hit (0
extra KC calls); E3 mint B from the SAME credential (aud=mcp-b); E4 rotation
write-back (KC rotated the RT on the refresh leg, newest persisted); E5
force_refresh re-mint (**2 KC calls** = the two-leg chain); E6 unconsented C →
refresh_failed_transient (KC returns invalid_request for a missing audience
scope → classified transient; credential SURVIVES either way); E7 flush→re-mint.
Gotcha: dev-mode Keycloak boot is slow on a loaded host — the script now waits on
kcadm auth (up to ~6 min) rather than a fixed sleep. Port 8091 (8090 = the dev
console).

Results — RUN 2026-09-03, ALL VERIFIED (exit 0): capture, E1–E7 and M1–M3
with the same call counts as above, now through the product client factory
(`json_http_client`, the JSON-preferring `Accept` posture the console and
nodes use). New check **D1**: authorization-server metadata discovery against
the realm issuer — a path-bearing issuer, the shape that failed before the
discovery rework — resolved in 3 probes to the realm's own token endpoint
with S256 advertised and the issuer confirmed. D1 calls `_fetch_as_metadata`
directly, so it also catches signature drift no unit test covers: it caught
the added `deadline` argument on the first run after that change.

## `oauth_user_e2e.py` + `oauth_user_e2e.sh` — per-user OAuth (`auth_type=oauth_user`), headless

The `oauth_user` sibling of `keycloak_e2e.py`. `oauth_user_e2e.sh` spins up
ephemeral Keycloak, configures the realm (a public PKCE-required client whose
tokens carry the resource identifier in `aud` through an audience mapper, a
second client without that mapper, a test user, anonymous dynamic client
registration), runs the harness, then tears down. The harness drives the REAL
console OAuth surface — `handle_mcp_oauth_authorize` →
`handle_mcp_oauth_callback` → `get_user_access_token_classified` →
`handle_mcp_oauth_revoke_connection`, mounted on the console's own routes with
the client `initialize_mcp_oauth_state` installs — so RFC 9728
protected-resource metadata, RFC 8414 authorization-server metadata for a
path-bearing realm issuer, the PKCE code exchange, the refresh grant and RFC
7009 revocation all happen on the wire. The "MCP server" is a local stub that
serves only the protected-resource metadata documents and records which
well-known location was read. No browser: the harness carries Keycloak's
cookies and submits its login (and, for a registered client, consent) form
itself. Ports: Keycloak 8092, resource-server stubs 8093 (path-specific) and
8094 (origin-level only), redirect base 8095 (8090 = the dev console, 8091 =
the obo harness).

```bash
./scripts/obo-e2e/oauth_user_e2e.sh
```

Results — RUN 2026-09-03, 13 VERIFIED / 2 FAILED (exit 1), reproduced on
`main` after the discovery rework merged; both failures are
product findings, not harness defects, and are listed below. VERIFIED: D1
discovery through the authorize handler — PRM read at the path-specific
location only, the realm issuer resolved in 3 probes to its
`openid-configuration`, issuer persisted on the row; C1 authorize URL (S256,
`resource=` the canonical row URL, callback derived from `redirect_base`); C2
consent — headless login, callback 302 to `return_url`, token row persisted as
ciphertext, `aud` carries the resource; C3 the stored token is live at Keycloak
(userinfo) and the callback made exactly one request, the code exchange; C4
cache hit (0 requests); C5 callback replay refused (state single-use, 0
requests); C6 refresh after a REAL clock expiry (the realm issues 90s tokens;
the harness waits ~30s for the product's 60s refresh-ahead window) — one token
POST, no re-discovery, refresh token rotated and written back, new token live;
C7 force_refresh (one more POST); C8 connections list without secret fields;
C9 revoke — 204, row gone, lookup `missing`, one POST to the realm's revocation
endpoint, and a refresh grant with the revoked token then fails
`invalid_grant` AT KEYCLOAK; N1 a PRM document declaring a sibling resource
refused (`PRM resource identifier does not match the server URL`), the origin
fallback probed and 404'd, no authorization-server traffic; N2 a real token
whose `aud` omits the resource refused at the callback with nothing persisted;
D2 the origin-level fallback accepted for a server that serves only there.

FAILED — product findings:

- **D4 issuer not persisted on a warm metadata cache** (#1081).
  `discover_authorization_server` returns from the in-process metadata-cache hit
  before reaching the block that writes `oauth_as_issuer_cached`, so any row
  whose authorization server is already cached (a second server on the same
  realm) never gets its issuer persisted: its callback, every refresh and the
  revoke re-read the resource server's PRM document for as long as the cache
  entry lives (24h), and a refresh then depends on the resource server being up
  to serve metadata the product already knows. Visible in N2's callback wire
  (a PRM GET before the token POST) as well. Predates this branch.
- **D3 dynamic client registration against Keycloak** (#1082). `register_dynamic_client`
  sends a `resource` member in the RFC 7591 body; Keycloak's OIDC registration
  endpoint rejects unknown members (`HTTP 400: Invalid json representation for
  OIDCClientRepresentation. Unrecognized field "resource"`), so
  `registration_mode=dcr` cannot work against Keycloak at all. With the member
  removed the same body registers (201, public client) and the consent flow
  completes on the registered client — verified by hand while building the
  harness. RFC 7591 §2 says the server MUST ignore metadata it does not
  understand, but `resource` is not registered client metadata either.

Gotchas: Keycloak marks its cookies `Secure` and a real browser still sends
them to a loopback origin over http (loopback is a secure context), but
`http.cookiejar` does not — the harness keeps the jar by hand. Keycloak ignores
RFC 8707 `resource=`, so the resource identifier reaches `aud` through an
audience mapper (the realm config does this). For anonymous DCR, Keycloak's
Trusted Hosts policy sees the docker gateway address, not loopback, as the
requester, so trust is judged on the registered redirect URI's host instead;
and its Allowed Client Scopes policy rejects a registration whose `scope`
names anything that is not a realm client scope (`openid` included), so the
DCR row is seeded with no scopes.

## Leg 1 — Entra (`entra_spike.py`) — NEEDS TENANT ACCESS

### Tenant / app-registration setup (one-time, ~15 min)

1. **Spike client app** (stands in for Turnstone's OIDC app registration):
   - New app registration, single tenant. Platform **Web**, redirect URI
     `http://localhost:8765/callback`. Create a **client secret**.
2. **Two resource apps** (stand in for MCP servers A and B):
   - New app registrations `spike-mcp-a`, `spike-mcp-b`. In each:
     **Expose an API** → set Application ID URI (`api://<guid>`) → add a scope
     (e.g. `mcp.access`).
3. **Delegated grants** (this is metaclassing's "proper tenant and app reg setup"):
   - On the spike client app → **API permissions** → add delegated permission to
     `spike-mcp-a` and `spike-mcp-b` scopes → **Grant admin consent**.
   - Optionally also add the spike client's app id to each resource app's
     `preAuthorizedApplications` (Expose an API → Add a client application) to
     compare against pure admin consent.
4. **Unconsented control** (for V5): a third resource app `spike-mcp-c` with an
   exposed API but NO permission granted to the spike client.

### Run

```bash
export ENTRA_TENANT_ID=... ENTRA_CLIENT_ID=... ENTRA_CLIENT_SECRET=...
export SPIKE_AUDIENCE_A=api://<a-guid> SPIKE_AUDIENCE_B=api://<b-guid>
export SPIKE_AUDIENCE_UNCONSENTED=api://<c-guid>   # optional (V5)
export SPIKE_RUN_OBO=1                              # optional (V6)
uv run python scripts/obo-e2e/entra_spike.py
```

A browser opens for one interactive login (any tenant user). Everything after is
non-interactive — that IS the feature.

### What each check pins down

| Check | Design assumption it verifies |
| --- | --- |
| V1 | `offline_access` on the login yields a client-bound RT (capture layer) |
| V2/V3 | ONE RT redeems for access tokens of DIFFERENT audiences (`scope=<aud>/.default`) — the load-bearing Entra behavior |
| V4 | rotation semantics → whether RT write-back on every mint is convenience or correctness-critical |
| V5 | unconsented audience fails `AADSTS65001 consent_required` → maps to the reconnect-rail fallback, never a silent failure |
| V6 | OBO jwt-bearer middle-tier variant works with the same app registration (comparison data only) |

Also record (manual): whether Conditional Access / MFA policies in the tenant
produce `interaction_required` on redemption — that's the fallback path's other
trigger.

### Results — RUN 2026-07-11 on a real tenant, ALL SIX VERIFIED

Tenant: personal default directory (Global Admin), user is an MSA member.
Setup via `entra_setup.sh setup`; V3 initially failed (see gotcha below),
passed after fixing the grant. Second run: V1-V6 all VERIFIED, exit 0.

| Check | Result |
| --- | --- |
| V1 offline_access login -> RT | VERIFIED (confidential client + PKCE, RT ~2KB) |
| V2 RT -> audience A token | VERIFIED (`aud=<A app guid>`, ~70 min TTL, new RT returned) |
| V3 SAME RT -> audience B token | **VERIFIED — the load-bearing claim: one RT, many audiences** |
| V4 rotation | VERIFIED: RT rotates on every redemption, but the OLD RT stays valid (reuse HTTP 200) -> write-back-newest is required; races are benign on Entra |
| V5 unconsented audience | VERIFIED: `invalid_grant` + `AADSTS65001` (error_codes=[65001]) -> clean mapping to the reconnect-rail fallback |
| V6 OBO jwt-bearer variant | VERIFIED: middle-tier shape also works with the same app registration |

**Operator gotcha (feeds #682 + product docs):** `az ad app permission
admin-consent` run immediately after SP creation SILENTLY skips
not-yet-propagated resource SPs — grant A landed, grant B didn't, and the only
symptom was AADSTS65001 at redemption. Verify grants after consent
(`oauth2PermissionGrants` filter on the client SP) or write them directly with
`az ad app permission grant --id <client> --api <resource> --scope <scope>`.
Product-side implication: a missing tenant grant for a NEW oauth_obo server
surfaces as AADSTS65001 -> the same reconnect-rail path as revocation; the
admin docs must say "grant first, then add the server".

## Leg 2 — Keycloak RFC 8693 (portability check) — runnable locally

Ephemeral `quay.io/keycloak/keycloak:26.3` (`start-dev`, port 8089), realm
`spike`, confidential client `turnstone` with **standard token exchange**
enabled, resource clients `mcp-a`/`mcp-b`, user `alice`. Pipeline mirrors the
product design for a generic-8693 IdP:

```
stored user RT --(refresh grant)--> user AT --(RFC 8693 exchange, audience=mcp-X)--> audience-scoped AT
```

i.e. the per-user credential stays ONE refresh token; per-server tokens are
minted via standard token exchange instead of Entra's multi-resource RT
redemption. Same substrate, different grant leg.

### Results — RUN 2026-07-11, VERIFIED (Keycloak 26.3, ephemeral)

```
alice ONE stored RT
  -> refresh grant                      -> user AT (azp=turnstone); RT ROTATED on refresh
  -> 8693 exchange audience=mcp-a scope=aud-mcp-a -> AT aud=mcp-a user=alice 300s, NO RT
  -> 8693 exchange audience=mcp-b scope=aud-mcp-b -> AT aud=mcp-b (same subject AT)
  negative control audience=mcp-c       -> invalid_client "Audience not found"
```

Findings that feed the design:
1. **One per-user credential -> N audience tokens: VERIFIED on a second IdP.**
   The substrate is portable; only the grant leg differs per IdP.
2. **Exchanged tokens are cache-shaped** (short TTL, no RT) — per-server
   `mcp_user_tokens` rows as short-lived mint cache is the right model.
3. **RT rotation happens here too** — newest-RT write-back on every redemption
   is a correctness requirement of the capture layer, not an Entra quirk.
4. **The IdP-side "delegated grant" has a per-IdP shape**: Entra = API
   permissions + admin consent; Keycloak = audience client scopes attached to
   the requester client (optional scopes activate via `scope=` at exchange).
   Operator runbooks are per-IdP (#682 pattern), code is not.
5. Gotchas hit: KC user needs a complete profile for direct grant ("Account is
   not fully set up"); optional audience scope must be requested explicitly or
   the exchange 400s with "Requested audience not available".

Repro (ephemeral, ~2 min):

```bash
docker run -d --name kc-obo-spike -p 127.0.0.1:8089:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak:26.3 start-dev
KC="docker exec kc-obo-spike /opt/keycloak/bin/kcadm.sh"
$KC config credentials --server http://localhost:8080 --realm master --user admin --password admin
$KC create realms -s realm=spike -s enabled=true
$KC create clients -r spike -s clientId=turnstone -s enabled=true -s publicClient=false \
  -s secret=spike-secret -s directAccessGrantsEnabled=true \
  -s 'attributes={"standard.token.exchange.enabled":"true"}'
$KC create clients -r spike -s clientId=mcp-a -s enabled=true -s publicClient=false -s secret=x
$KC create clients -r spike -s clientId=mcp-b -s enabled=true -s publicClient=false -s secret=x
$KC create users -r spike -s username=alice -s enabled=true -s email=a@s.test \
  -s emailVerified=true -s firstName=A -s lastName=S
$KC set-password -r spike --username alice --new-password alice-pw
TURNSTONE_UUID=$($KC get clients -r spike -q clientId=turnstone --fields id --format csv --noquotes)
for t in mcp-a mcp-b; do
  SID=$($KC create client-scopes -r spike -s name=aud-$t -s protocol=openid-connect -i)
  $KC create client-scopes/$SID/protocol-mappers/models -r spike -s name=aud-$t \
    -s protocol=openid-connect -s protocolMapper=oidc-audience-mapper \
    -s "config={\"included.client.audience\":\"$t\",\"access.token.claim\":\"true\"}"
  $KC update clients/$TURNSTONE_UUID/optional-client-scopes/$SID -r spike
done
# then: password grant -> refresh grant -> token-exchange with
# grant_type=urn:ietf:params:oauth:grant-type:token-exchange,
# subject_token=<user AT>, subject_token_type=...:access_token,
# audience=mcp-a, scope=aud-mcp-a
```
