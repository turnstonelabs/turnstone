#!/usr/bin/env bash
# Per-user OAuth (auth_type=oauth_user) end-to-end: spin up ephemeral Keycloak,
# configure the realm, run oauth_user_e2e.py against the REAL console OAuth
# handlers, tear down. Fully headless — the harness submits Keycloak's login
# form itself. Manual test tooling, not run in CI.
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root (uv run needs it)

CONTAINER=kc-oauth-user-e2e
PORT=8092                # 8090 = the dev console, 8091 = the obo harness
STUB_PORT=8093           # resource-server stub: path-specific PRM (+ the wrong-resource negative)
STUB_ORIGIN_PORT=8094    # resource-server stub: origin-level PRM only (the fallback)
# The console's externally-visible base. Nothing listens there: the harness
# plays the browser and hands Keycloak's redirect to the in-process app.
REDIRECT_BASE=http://127.0.0.1:8095
KC="docker exec $CONTAINER /opt/keycloak/bin/kcadm.sh"
ISSUER="http://127.0.0.1:${PORT}/realms/spike"
RESOURCE="http://127.0.0.1:${STUB_PORT}/mcp"
CALLBACK="${REDIRECT_BASE}/v1/api/mcp/oauth/callback"

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo ">> starting Keycloak 26.3 (ephemeral)..."
docker run -d --name "$CONTAINER" -p "127.0.0.1:${PORT}:8080" \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak:26.3 start-dev >/dev/null

echo ">> waiting for Keycloak (dev-mode boot can take a few minutes on a loaded host)..."
# Wait on kcadm auth succeeding directly — more reliable than the host HTTP port,
# and generous enough for a resource-starved boot (up to ~6 min).
ready=""
for _ in $(seq 1 90); do
  if $KC config credentials --server http://localhost:8080 --realm master \
      --user admin --password admin >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 4
done
[ -n "$ready" ] || { echo "Keycloak did not become ready in time"; docker logs "$CONTAINER" 2>&1 | tail -15; exit 1; }

echo ">> configuring realm 'spike'..."
# Access tokens live 90s so the refresh-after-expiry check waits for a real
# clock expiry (the product refreshes 60s ahead) instead of forging one.
$KC create realms -s realm=spike -s enabled=true -s accessTokenLifespan=90 >/dev/null
# Public client, PKCE S256 required: the shape the per-user flow prefers, with
# no client secret to hold. No direct grant — the only way in is the browser.
for c in turnstone-mcp turnstone-mcp-noaud; do
  $KC create clients -r spike -s clientId=$c -s enabled=true -s publicClient=true \
    -s standardFlowEnabled=true -s directAccessGrantsEnabled=false \
    -s "redirectUris=[\"$CALLBACK\"]" \
    -s 'attributes={"pkce.code.challenge.method":"S256"}' >/dev/null
done
$KC create users -r spike -s username=e2e-user -s enabled=true -s email=e2e@spike.test \
  -s emailVerified=true -s firstName=E2E -s lastName=User >/dev/null
$KC set-password -r spike --username e2e-user --new-password e2e-pw >/dev/null

# Keycloak ignores RFC 8707 resource=, so the resource identifier reaches the
# token's aud through an audience mapper. Attached to turnstone-mcp only —
# turnstone-mcp-noaud keeps issuing tokens without it, which the callback must
# refuse (N2) — and made a realm default so a dynamically registered client
# inherits it (D3).
SID=$($KC create client-scopes -r spike -s name=mcp-resource -s protocol=openid-connect -i)
$KC create "client-scopes/$SID/protocol-mappers/models" -r spike -s name=mcp-resource \
  -s protocol=openid-connect -s protocolMapper=oidc-audience-mapper \
  -s "config={\"included.custom.audience\":\"$RESOURCE\",\"access.token.claim\":\"true\"}" >/dev/null
CID=$($KC get clients -r spike -q clientId=turnstone-mcp --fields id --format csv --noquotes)
$KC update "clients/$CID/default-client-scopes/$SID" -r spike >/dev/null
$KC update "default-default-client-scopes/$SID" -r spike >/dev/null

# Anonymous dynamic client registration (D3). Keycloak's Trusted Hosts policy
# sees the docker gateway, not loopback, as the requester, so trust is judged
# on the registered redirect URI's host instead.
TRUSTED=$($KC get components -r spike \
  -q type=org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy \
  --fields id,providerId,subType --format csv --noquotes \
  | awk -F, '$2=="trusted-hosts" && $3=="anonymous" {print $1}')
$KC update "components/$TRUSTED" -r spike -s 'config."trusted-hosts"=["127.0.0.1"]' \
  -s 'config."host-sending-registration-request-must-match"=["false"]' \
  -s 'config."client-uris-must-match"=["true"]' >/dev/null

echo ">> running the product e2e harness..."
export KC_ISSUER="$ISSUER" KC_CLIENT_ID=turnstone-mcp KC_NOAUD_CLIENT_ID=turnstone-mcp-noaud
export KC_USER=e2e-user KC_PASSWORD=e2e-pw
export MCP_STUB_PORT="$STUB_PORT" MCP_STUB_ORIGIN_PORT="$STUB_ORIGIN_PORT" REDIRECT_BASE
uv run python scripts/obo-e2e/oauth_user_e2e.py
