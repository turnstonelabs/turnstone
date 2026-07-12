#!/usr/bin/env bash
# Entra spike setup for entra_spike.py (#551 re-scope boundary spike).
# Manual test tooling — not run in CI. Creates throwaway Entra app registrations.
#
#   ./entra_setup.sh setup     create app registrations + consent + .env
#   ./entra_setup.sh cleanup   delete everything it created (incl. .env)
#
# Creates in the logged-in tenant (az login first):
#   spike-turnstone   confidential client (stands in for Turnstone's OIDC app)
#   spike-mcp-a/b     resource apps exposing scope mcp.access, admin-consented
#   spike-mcp-c       resource app with NO grant to the client (V5 control)
# Requires: the logged-in user can create apps + grant admin consent
# (Global Admin on a personal tenant qualifies).

set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE=".env"
NAMES=(spike-turnstone spike-mcp-a spike-mcp-b spike-mcp-c)

log() { printf '>> %s\n' "$*"; }

graph_patch_api() { # $1=appId  $2=scope-uuid  $3=display-name
  local obj_id
  obj_id=$(az ad app show --id "$1" --query id -o tsv)
  az rest --method PATCH \
    --url "https://graph.microsoft.com/v1.0/applications/${obj_id}" \
    --headers 'Content-Type=application/json' \
    --body "{
      \"identifierUris\": [\"api://$1\"],
      \"api\": {
        \"requestedAccessTokenVersion\": 2,
        \"oauth2PermissionScopes\": [{
          \"id\": \"$2\",
          \"value\": \"mcp.access\",
          \"type\": \"Admin\",
          \"isEnabled\": true,
          \"adminConsentDisplayName\": \"Access $3\",
          \"adminConsentDescription\": \"Spike scope for $3\"
        }]
      }
    }"
}

make_resource_app() { # $1=display-name ; echoes "appId scopeId"
  local app_id scope_id
  app_id=$(az ad app create --display-name "$1" \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)
  scope_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
  graph_patch_api "$app_id" "$scope_id" "$1" >/dev/null
  az ad sp create --id "$app_id" >/dev/null 2>&1 || true
  echo "$app_id $scope_id"
}

cmd_setup() {
  local tenant_id
  tenant_id=$(az account show --query tenantId -o tsv)
  log "tenant: ${tenant_id}"

  log "creating resource apps (a, b, c)..."
  read -r APP_A SCOPE_A <<<"$(make_resource_app spike-mcp-a)"
  read -r APP_B SCOPE_B <<<"$(make_resource_app spike-mcp-b)"
  read -r APP_C _ <<<"$(make_resource_app spike-mcp-c)"
  log "  a=${APP_A}  b=${APP_B}  c=${APP_C} (c stays unconsented)"

  log "creating confidential client spike-turnstone..."
  CLIENT_ID=$(az ad app create --display-name spike-turnstone \
    --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "http://localhost:8765/callback" \
    --query appId -o tsv)
  az ad sp create --id "$CLIENT_ID" >/dev/null 2>&1 || true
  SECRET=$(az ad app credential reset --id "$CLIENT_ID" \
    --display-name spike --years 1 --query password -o tsv 2>/dev/null)

  log "adding delegated permissions (a, b — NOT c)..."
  az ad app permission add --id "$CLIENT_ID" \
    --api "$APP_A" --api-permissions "${SCOPE_A}=Scope" 2>/dev/null
  az ad app permission add --id "$CLIENT_ID" \
    --api "$APP_B" --api-permissions "${SCOPE_B}=Scope" 2>/dev/null

  log "granting admin consent (retries while SPs propagate)..."
  local ok=""
  for i in 1 2 3 4 5; do
    if az ad app permission admin-consent --id "$CLIENT_ID" 2>/dev/null; then
      ok=1; break
    fi
    log "  not yet (attempt $i) — waiting 15s"
    sleep 15
  done
  [ -n "$ok" ] || { log "admin-consent failed after retries — grant manually in the portal (API permissions blade) and re-run the spike"; }

  umask 177
  cat > "$ENV_FILE" <<EOF
export ENTRA_TENANT_ID=${tenant_id}
export ENTRA_CLIENT_ID=${CLIENT_ID}
export ENTRA_CLIENT_SECRET=${SECRET}
export SPIKE_AUDIENCE_A=api://${APP_A}
export SPIKE_AUDIENCE_B=api://${APP_B}
export SPIKE_AUDIENCE_UNCONSENTED=api://${APP_C}
export SPIKE_RUN_OBO=1
EOF
  log "wrote ${ENV_FILE} (chmod 600). Next:"
  log "  source scripts/obo-e2e/.env && uv run python scripts/obo-e2e/entra_spike.py"
  log "cleanup later with: ./entra_setup.sh cleanup"
}

cmd_cleanup() {
  for name in "${NAMES[@]}"; do
    for app_id in $(az ad app list --display-name "$name" --query '[].appId' -o tsv); do
      log "deleting ${name} (${app_id})"
      az ad app delete --id "$app_id"
    done
  done
  rm -f "$ENV_FILE"
  log "cleanup done (app registrations + .env removed)"
}

case "${1:-}" in
  setup)   cmd_setup ;;
  cleanup) cmd_cleanup ;;
  *) echo "usage: $0 setup|cleanup"; exit 2 ;;
esac
