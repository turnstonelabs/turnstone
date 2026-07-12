#!/usr/bin/env bash
# OSS-path (RFC 8693) end-to-end: spin up ephemeral Keycloak, configure the
# realm, run keycloak_e2e.py against the REAL Turnstone mint engine, tear down.
# Fully headless — no browser. Manual test tooling, not run in CI.
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root (uv run needs it)

CONTAINER=kc-obo-e2e
PORT=8091
KC="docker exec $CONTAINER /opt/keycloak/bin/kcadm.sh"

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
$KC create realms -s realm=spike -s enabled=true >/dev/null
# Confidential client with standard token exchange (the RFC 8693 leg) + direct
# access grant (headless password login to fetch the user's refresh token).
$KC create clients -r spike -s clientId=turnstone -s enabled=true -s publicClient=false \
  -s secret=spike-secret -s directAccessGrantsEnabled=true \
  -s 'attributes={"standard.token.exchange.enabled":"true"}' >/dev/null
for t in mcp-a mcp-b mcp-c; do
  $KC create clients -r spike -s clientId=$t -s enabled=true -s publicClient=false -s secret=x >/dev/null
done
$KC create users -r spike -s username=e2e-user -s enabled=true -s email=e2e@spike.test \
  -s emailVerified=true -s firstName=E2E -s lastName=User >/dev/null
$KC set-password -r spike --username e2e-user --new-password e2e-pw >/dev/null

TURNSTONE_UUID=$($KC get clients -r spike -q clientId=turnstone --fields id --format csv --noquotes)
# Audience client scopes for mcp-a and mcp-b ONLY (mcp-c stays unconsented → E6).
for t in mcp-a mcp-b; do
  SID=$($KC create client-scopes -r spike -s name=aud-$t -s protocol=openid-connect -i)
  $KC create "client-scopes/$SID/protocol-mappers/models" -r spike -s name=aud-$t \
    -s protocol=openid-connect -s protocolMapper=oidc-audience-mapper \
    -s "config={\"included.client.audience\":\"$t\",\"access.token.claim\":\"true\"}" >/dev/null
  $KC update "clients/$TURNSTONE_UUID/optional-client-scopes/$SID" -r spike >/dev/null
done

echo ">> running the product e2e harness..."
export KC_TOKEN_ENDPOINT="http://127.0.0.1:${PORT}/realms/spike/protocol/openid-connect/token"
export KC_ISSUER="http://127.0.0.1:${PORT}/realms/spike"
export KC_CLIENT_ID=turnstone KC_CLIENT_SECRET=spike-secret
export KC_USER=e2e-user KC_PASSWORD=e2e-pw
export AUD_A=mcp-a SCOPE_A=aud-mcp-a AUD_B=mcp-b SCOPE_B=aud-mcp-b AUD_C=mcp-c
uv run python scripts/obo-e2e/keycloak_e2e.py
