#!/usr/bin/env bash
# Live end-to-end check of secret:// references against a Vault-compatible store: start an
# ephemeral Vault and/or OpenBao development server, configure it with the server's own CLI
# (KV v2 secrets, a read policy, approle and jwt logins), run vault_e2e.py against the REAL
# resolver, registry and MCP code, tear down. Manual test tooling, not run in CI.
#
#   scripts/secret-refs-e2e/vault_e2e.sh [vault|openbao|both]    (default: both)
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

VAULT_IMAGE=${VAULT_IMAGE:-hashicorp/vault:2.1.2}
OPENBAO_IMAGE=${OPENBAO_IMAGE:-openbao/openbao:2.5.0}
PORT=${PORT:-8210}
ROOT_TOKEN=e2e-root
# PYTHON wins when set; otherwise the repo's own environment, since "uv run" can drop the
# extras a full sync installed. Without a venv, uv installs the test extra (pytest).
if [ -n "${PYTHON:-}" ]; then
  PY=$PYTHON
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY="uv run --extra test python"
fi

CONTAINER=""
WORK=""
cleanup() {
  [ -n "$CONTAINER" ] && docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  [ -n "$WORK" ] && rm -rf "$WORK" || true
}
trap cleanup EXIT

run_one() {
  local flavor=$1 image cli
  case "$flavor" in
    vault)   image=$VAULT_IMAGE;   cli=vault ;;
    openbao) image=$OPENBAO_IMAGE; cli=bao ;;
    *) echo "unknown store '$flavor' (vault, openbao or both)"; exit 2 ;;
  esac
  CONTAINER=secret-refs-e2e-$flavor
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  WORK=$(mktemp -d)
  chmod 700 "$WORK"

  # The store's CLI inside the container, as root: the operator's side of every check.
  store() {
    docker exec -i -e VAULT_ADDR=http://127.0.0.1:8200 -e BAO_ADDR=http://127.0.0.1:8200 \
      -e VAULT_TOKEN=$ROOT_TOKEN -e BAO_TOKEN=$ROOT_TOKEN "$CONTAINER" "$cli" "$@"
  }

  echo ">> [$flavor] starting $image (development server, in memory)..."
  docker run -d --name "$CONTAINER" -p "127.0.0.1:${PORT}:8200" \
    -e VAULT_DEV_ROOT_TOKEN_ID=$ROOT_TOKEN -e BAO_DEV_ROOT_TOKEN_ID=$ROOT_TOKEN \
    -e SKIP_SETCAP=true "$image" server -dev >/dev/null

  echo ">> [$flavor] waiting for the server..."
  local ready=""
  for _ in $(seq 1 60); do
    if store status </dev/null >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  [ -n "$ready" ] || { echo "the $flavor server did not start"; docker logs "$CONTAINER" 2>&1 | tail -20; exit 1; }
  # The development banner, with the unseal key V9 needs, is printed after the API answers.
  local unseal_key=""
  for _ in $(seq 1 30); do
    unseal_key=$(docker logs "$CONTAINER" 2>&1 | sed -n 's/^Unseal Key: //p' | head -1)
    [ -n "$unseal_key" ] && break
    sleep 1
  done
  [ -n "$unseal_key" ] || { echo "no unseal key in the $flavor server's log"; exit 1; }

  echo ">> [$flavor] configuring secrets, a read policy, approle and jwt logins..."
  store kv put secret/turnstone/openai api_key=sk-e2e-v1 </dev/null >/dev/null
  store kv put secret/turnstone/mcp authorization="Bearer mcp-e2e-v1" </dev/null >/dev/null
  store kv put secret/turnstone/rotating api_key=sk-rotate-v1 </dev/null >/dev/null
  store kv put secret/other/denied api_key=sk-denied </dev/null >/dev/null
  # A field written from a file keeps the file's trailing newline; one with a trailing space
  # cannot be sent in a header (V8).
  printf 'sk-e2e-newline\n' | docker exec -i "$CONTAINER" sh -c 'cat > /tmp/key.txt'
  store kv put secret/turnstone/newline api_key=@/tmp/key.txt </dev/null >/dev/null
  store kv put secret/turnstone/padded api_key="sk-e2e-padded " </dev/null >/dev/null
  printf 'path "secret/data/turnstone/*" {\n  capabilities = ["read"]\n}\n' \
    | store policy write turnstone-read - >/dev/null

  store auth enable approle </dev/null >/dev/null
  store write auth/approle/role/turnstone token_policies=turnstone-read token_ttl=1h \
    secret_id_ttl=0 </dev/null >/dev/null
  store write auth/approle/role/turnstone-short token_policies=turnstone-read token_ttl=5s \
    token_max_ttl=5s secret_id_ttl=0 </dev/null >/dev/null
  local role_id secret_id short_role_id short_secret_id
  role_id=$(store read -field=role_id auth/approle/role/turnstone/role-id </dev/null)
  secret_id=$(store write -f -field=secret_id auth/approle/role/turnstone/secret-id </dev/null)
  short_role_id=$(store read -field=role_id auth/approle/role/turnstone-short/role-id </dev/null)
  short_secret_id=$(store write -f -field=secret_id auth/approle/role/turnstone-short/secret-id </dev/null)

  # jwt logins verify a workload token against this key pair's public half; the harness signs
  # the token with the private half.
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$WORK/jwt.key" 2>/dev/null
  openssl pkey -in "$WORK/jwt.key" -pubout -out "$WORK/jwt.pub" 2>/dev/null
  store auth enable jwt </dev/null >/dev/null
  store write auth/jwt/config jwt_validation_pubkeys=- <"$WORK/jwt.pub" >/dev/null
  store write auth/jwt/role/turnstone role_type=jwt bound_audiences=turnstone-e2e user_claim=sub \
    token_policies=turnstone-read token_ttl=1h </dev/null >/dev/null

  # The process reads this file, never the operator's own config.toml.
  (
    umask 077
    cat >"$WORK/config.toml" <<EOF
[secrets]
cache_ttl_seconds = 300

[secrets.vault]
address = "http://127.0.0.1:${PORT}"
auth = "approle"
role_id = "${role_id}"
secret_id = "${secret_id}"
EOF
  )

  echo ">> [$flavor] running the harness against the real code..."
  TURNSTONE_CONFIG="$WORK/config.toml" STORE_FLAVOR=$flavor \
    STORE_ADDR="http://127.0.0.1:${PORT}" STORE_ROOT_TOKEN=$ROOT_TOKEN \
    STORE_CONTAINER="$CONTAINER" STORE_CLI=$cli STORE_UNSEAL_KEY="$unseal_key" STORE_WORK="$WORK" \
    SHORT_ROLE_ID="$short_role_id" SHORT_SECRET_ID="$short_secret_id" \
    $PY scripts/secret-refs-e2e/vault_e2e.py

  echo ">> [$flavor] running the opt-in live test (tests/test_secret_refs_live.py)..."
  TURNSTONE_CONFIG="$WORK/config.toml" TURNSTONE_LIVE_VAULT_ADDR="http://127.0.0.1:${PORT}" \
    TURNSTONE_LIVE_VAULT_ROLE_ID="$role_id" TURNSTONE_LIVE_VAULT_SECRET_ID="$secret_id" \
    TURNSTONE_LIVE_VAULT_EXPECT=sk-e2e-v1 \
    $PY -m pytest tests/test_secret_refs_live.py -q -p no:cacheprovider

  cleanup
  CONTAINER=""
  WORK=""
}

case "${1:-both}" in
  both) run_one vault; run_one openbao ;;
  *) run_one "$1" ;;
esac
