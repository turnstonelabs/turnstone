#!/usr/bin/env bash
# Interactive builder for a Docker-based Turnstone deployment.
#
# Produces every file needed to run the stack in this folder:
#   compose.yaml            pulled production stack (ghcr.io images)
#   compose.override.yaml   deployment-specific service adjustments
#   tls.compose.yaml        service-to-service mTLS overlay (optional)
#   Caddyfile               browser TLS termination for the dashboard
#   caddy/Dockerfile        Caddy build with a DNS-challenge plugin (optional)
#   config/turnstone-oidc.env  OIDC single sign-on settings (optional)
#   .env                    secrets and image tag
#
# After a successful run:  cd into this folder and `docker compose up -d`.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

SRC_COMPOSE="$REPO_DIR/turnstone/deploy/compose.yaml"
SRC_CADDYFILE="$REPO_DIR/turnstone/deploy/Caddyfile"
SRC_SEARXNG_DIR="$REPO_DIR/turnstone/deploy/searxng"
SRC_TLS_OVERLAY="$REPO_DIR/deploy/docker-compose.tls.yml"

OUT_COMPOSE="$SCRIPT_DIR/compose.yaml"
OUT_OVERRIDE="$SCRIPT_DIR/compose.override.yaml"
OUT_TLS_OVERLAY="$SCRIPT_DIR/tls.compose.yaml"
OUT_CADDYFILE="$SCRIPT_DIR/Caddyfile"
OUT_CADDY_DIR="$SCRIPT_DIR/caddy"
OUT_CONFIG_DIR="$SCRIPT_DIR/config"
OUT_OIDC_ENV="$OUT_CONFIG_DIR/turnstone-oidc.env"
OUT_SEARXNG_DIR="$SCRIPT_DIR/searxng"
OUT_ENV="$SCRIPT_DIR/.env"
OUT_ANSWERS="$SCRIPT_DIR/setup-production.env"

# Persistent answers file state. load_answers_file() sets AUTORUN from the
# file; write_answers_file() preserves it unchanged after a run.
AUTORUN="false"
declare -A _ANSWERS        # populated by each prompt helper during the run
declare -A _FILE_DEFAULTS  # answers-file values, used as prompt defaults

IMAGE_REPO_API="https://api.github.com/repos/turnstonelabs/turnstone"

if [ -t 1 ]; then
    BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
info() { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
# Returns true when the script must not attempt interactive prompts.
_no_tty() { [ "$AUTORUN" = "true" ] || [ ! -r /dev/tty ]; }

on_error() {
    local rc=$?
    printf '\n%serror:%s setup-production.sh stopped unexpectedly (exit %s). Review the output above and re-run.\n' \
        "$RED" "$RESET" "$rc" >&2
}
trap on_error ERR

# ---------------------------------------------------------------------------
# Prompt helpers. Every value is resolved through three precedence tiers
# (highest to lowest):
#   1. Environment variable exported by the user before invocation
#      (e.g. TURNSTONE_SETUP_OIDC=n ./setup-production.sh) — used as-is,
#      the prompt is skipped.
#   2. Value from the answers file (_FILE_DEFAULTS). With AUTORUN=true (or
#      no readable TTY) it is used as-is and the prompt is skipped; with
#      AUTORUN=false it only becomes the displayed default of the prompt.
#   3. Interactive prompt / built-in default.
# ---------------------------------------------------------------------------
ask() {
    local prompt="$1" envvar="$2" default="${3:-y}" ans hint
    if [ -n "$envvar" ] && [[ -v $envvar ]]; then
        case "${!envvar}" in
            [Yy]*|1|true|TRUE) _ANSWERS["$envvar"]="y"; return 0 ;;
            *)                  _ANSWERS["$envvar"]="n"; return 1 ;;
        esac
    fi
    # Answers-file value becomes the default answer (tier 2/3).
    if [ -n "$envvar" ] && [ -n "${_FILE_DEFAULTS[$envvar]:-}" ]; then
        case "${_FILE_DEFAULTS[$envvar]}" in
            [Yy]*|1|true|TRUE) default="y" ;;
            *)                 default="n" ;;
        esac
    fi
    [ "$default" = y ] && hint="Y/n" || hint="y/N"
    if _no_tty; then
        warn "non-interactive shell; assuming '$default' for: $prompt"
        if [ "$default" = y ]; then
            [ -n "$envvar" ] && _ANSWERS["$envvar"]="y"; return 0
        else
            [ -n "$envvar" ] && _ANSWERS["$envvar"]="n"; return 1
        fi
    fi
    printf '%s%s%s [%s] ' "$BOLD" "$prompt" "$RESET" "$hint" >/dev/tty
    read -r ans </dev/tty || ans=""
    ans="${ans:-$default}"
    case "$ans" in
        [Yy]*) [ -n "$envvar" ] && _ANSWERS["$envvar"]="y"; return 0 ;;
        *)     [ -n "$envvar" ] && _ANSWERS["$envvar"]="n"; return 1 ;;
    esac
}

prompt_value() {
    local outvar="$1" envvar="$2" prompt="$3" default="${4:-}" value=""
    if [ -n "$envvar" ] && [[ -v $envvar ]]; then
        printf -v "$outvar" '%s' "${!envvar}"
        [ -n "$envvar" ] && _ANSWERS["$envvar"]="${!envvar}"
        return
    fi
    # Answers-file value becomes the bracketed default (tier 2/3).
    if [ -n "$envvar" ] && [ -n "${_FILE_DEFAULTS[$envvar]:-}" ]; then
        default="${_FILE_DEFAULTS[$envvar]}"
    fi
    if _no_tty; then
        if [ -n "$default" ]; then
            warn "non-interactive shell; using default for: $prompt"
            printf -v "$outvar" '%s' "$default"
            [ -n "$envvar" ] && _ANSWERS["$envvar"]="$default"
            return
        fi
        die "missing required input for: $prompt. Set ${envvar}."
    fi
    while :; do
        if [ -n "$default" ]; then
            printf '%s%s%s [%s]: ' "$BOLD" "$prompt" "$RESET" "$default" >/dev/tty
        else
            printf '%s%s%s: ' "$BOLD" "$prompt" "$RESET" >/dev/tty
        fi
        read -r value </dev/tty || value=""
        value="${value:-$default}"
        if [ -n "$value" ]; then
            printf -v "$outvar" '%s' "$value"
            [ -n "$envvar" ] && _ANSWERS["$envvar"]="$value"
            return
        fi
        printf 'A value is required.\n' >/dev/tty
    done
}

prompt_optional() {
    local outvar="$1" envvar="$2" prompt="$3" default="" value=""
    if [ -n "$envvar" ] && [[ -v $envvar ]]; then
        printf -v "$outvar" '%s' "${!envvar}"
        [ -n "$envvar" ] && _ANSWERS["$envvar"]="${!envvar}"
        return
    fi
    # Answers-file value becomes the bracketed default (tier 2/3).
    if [ -n "$envvar" ] && [ -n "${_FILE_DEFAULTS[$envvar]:-}" ]; then
        default="${_FILE_DEFAULTS[$envvar]}"
    fi
    if _no_tty; then
        printf -v "$outvar" '%s' "$default"
        [ -n "$envvar" ] && _ANSWERS["$envvar"]="$default"
        return
    fi
    if [ -n "$default" ]; then
        printf '%s%s%s [%s]: ' "$BOLD" "$prompt" "$RESET" "$default" >/dev/tty
    else
        printf '%s%s%s [leave blank to skip]: ' "$BOLD" "$prompt" "$RESET" >/dev/tty
    fi
    read -r value </dev/tty || value=""
    value="${value:-$default}"
    printf -v "$outvar" '%s' "$value"
    [ -n "$envvar" ] && _ANSWERS["$envvar"]="$value"
}

prompt_choice() {
    # prompt_choice OUTVAR ENVVAR PROMPT DEFAULT CHOICE...
    local outvar="$1" envvar="$2" prompt="$3" default="$4" value choice
    shift 4
    if [ -n "$envvar" ] && [[ -v $envvar ]]; then
        for choice in "$@"; do
            if [ "${!envvar}" = "$choice" ]; then
                printf -v "$outvar" '%s' "$choice"
                [ -n "$envvar" ] && _ANSWERS["$envvar"]="$choice"
                return
            fi
        done
        die "invalid value '${!envvar}' for ${envvar} (expected one of: $*)"
    fi
    # Answers-file value becomes the default choice (tier 2/3), validated
    # against the choice list so a stale/invalid file value cannot leak in.
    if [ -n "$envvar" ] && [ -n "${_FILE_DEFAULTS[$envvar]:-}" ]; then
        local _fd_valid=0
        for choice in "$@"; do
            if [ "${_FILE_DEFAULTS[$envvar]}" = "$choice" ]; then
                _fd_valid=1
                break
            fi
        done
        if [ "$_fd_valid" = 1 ]; then
            default="${_FILE_DEFAULTS[$envvar]}"
        else
            warn "ignoring invalid answers-file value '${_FILE_DEFAULTS[$envvar]}' for ${envvar} (expected one of: $*)"
        fi
    fi
    if _no_tty; then
        warn "non-interactive shell; using default '$default' for: $prompt"
        printf -v "$outvar" '%s' "$default"
        [ -n "$envvar" ] && _ANSWERS["$envvar"]="$default"
        return
    fi
    while :; do
        printf '%s%s%s (%s) [%s]: ' "$BOLD" "$prompt" "$RESET" "$(IFS='/'; echo "$*")" "$default" >/dev/tty
        read -r value </dev/tty || value=""
        value="${value:-$default}"
        for choice in "$@"; do
            if [ "$value" = "$choice" ]; then
                printf -v "$outvar" '%s' "$choice"
                [ -n "$envvar" ] && _ANSWERS["$envvar"]="$choice"
                return
            fi
        done
        printf 'Choose one of: %s\n' "$*" >/dev/tty
    done
}

gen_hex() {
    if have openssl; then
        openssl rand -hex 32
    elif have python3; then
        python3 -c "import secrets; print(secrets.token_hex(32))"
    else
        head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

# ---------------------------------------------------------------------------
# Answers file: load prior answers (Bug fix: preserves existing secrets) and
# write current answers back after a successful run.
# ---------------------------------------------------------------------------

load_answers_file() {
    if [ ! -f "$OUT_ANSWERS" ]; then
        return
    fi
    local _line _key _value
    while IFS= read -r _line; do
        # Skip comments and blank lines.
        [[ "$_line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${_line//[[:space:]]/}" ]]  && continue
        [[ "$_line" == *=* ]] || continue
        _key="${_line%%=*}"
        _value="${_line#*=}"
        if [ "$_key" = "AUTORUN" ]; then
            AUTORUN="$_value"
            continue
        fi
        # Only load non-empty values. File values are kept separate from
        # the environment so they act as prompt defaults (precedence 2),
        # never masquerading as user-exported variables (precedence 1).
        if [ -n "$_value" ] && [ -n "$_key" ]; then
            _FILE_DEFAULTS["$_key"]="$_value"
        fi
    done <"$OUT_ANSWERS"
    if [ "$AUTORUN" = "true" ]; then
        info "AUTORUN=true: running non-interactively using $OUT_ANSWERS"
    fi
}

write_answers_file() {
    # Preserve the AUTORUN flag exactly as set by the user — never modify it.
    local _autorun="false"
    if [ -f "$OUT_ANSWERS" ]; then
        local _al
        while IFS= read -r _al; do
            if [[ "$_al" =~ ^AUTORUN=(.*)$ ]]; then
                _autorun="${BASH_REMATCH[1]}"
                break
            fi
        done <"$OUT_ANSWERS"
    fi
    info "Saving answers to $OUT_ANSWERS"
    local _a  # shorthand: print one answer line safely
    _a() { printf '%s=%s\n' "$1" "${_ANSWERS[$1]:-}"; }
    (
        umask 177
        {
        cat <<'HDR'
# Turnstone production deployment answers file.
# Generated and updated by setup-production.sh after each successful run.
# Secrets are stored here — keep this file private (chmod 600, gitignored).
#
# AUTORUN flag
#   false (default): script prompts interactively; values below are pre-filled defaults.
#   true: fully non-interactive — all required values for enabled sections must be filled
#         in below; a missing required value causes an immediate error naming the variable.
#   Only YOU should change this flag; setup-production.sh never modifies it.
#
# Precedence (highest to lowest):
#   1. Exported shell environment variable  (e.g. TURNSTONE_SETUP_OIDC=n ./setup-production.sh)
#   2. Value in this file
#   3. Interactive prompt / built-in default
HDR
        printf 'AUTORUN=%s\n' "$_autorun"
        cat <<'S1'

# ---------------------------------------------------------------------------
# Deployment mode
# ---------------------------------------------------------------------------
# production or development  (default: production)
# Choosing development ends the script; use ./run.sh for the dev stack.
S1
        _a TURNSTONE_SETUP_MODE
        cat <<'S2'

# ---------------------------------------------------------------------------
# Image tag
# ---------------------------------------------------------------------------
# Track the rolling image or pin a release.  Values: latest | pinned  (default: latest)
S2
        _a TURNSTONE_SETUP_IMAGE_CHANNEL
        printf '%s\n' "# Specific release tag when IMAGE_CHANNEL=pinned (e.g. v1.2.3)."
        _a TURNSTONE_SETUP_IMAGE_TAG
        cat <<'S2A'

# ---------------------------------------------------------------------------
# Server nodes
# ---------------------------------------------------------------------------
# How many turnstone-server nodes to run. Positive integer  (default: 1)
S2A
        _a TURNSTONE_SETUP_NODE_COUNT
        cat <<'S3'

# ---------------------------------------------------------------------------
# OIDC single sign-on (optional)
# ---------------------------------------------------------------------------
# Enable OIDC SSO?  Values: y | n  (default: n)
S3
        _a TURNSTONE_SETUP_OIDC
        printf '%s\n' "# Identity provider type.  Values: authentik | generic  (default: generic)"
        _a TURNSTONE_SETUP_OIDC_PROVIDER
        cat <<'S3A'
# --- Authentik path (only when OIDC_PROVIDER=authentik) ---
# Authentik base URL (e.g. https://authentik.example.com)
S3A
        _a TURNSTONE_SETUP_AUTHENTIK_URL
        printf '%s\n' "# Authentik application slug"
        _a TURNSTONE_SETUP_AUTHENTIK_SLUG
        cat <<'S3B'
# --- Generic path (only when OIDC_PROVIDER=generic) ---
# OIDC issuer URL (must serve /.well-known/openid-configuration)
S3B
        _a TURNSTONE_OIDC_ISSUER
        cat <<'S3C'
# --- Both provider paths ---
# OIDC client ID
S3C
        _a TURNSTONE_OIDC_CLIENT_ID
        printf '%s\n' "# OIDC client secret (sensitive — file is kept 600)"
        _a TURNSTONE_OIDC_CLIENT_SECRET
        printf '%s\n' "# Login button label (generic only, default: SSO)"
        _a TURNSTONE_OIDC_PROVIDER_NAME
        printf '%s\n' "# OAuth scopes (generic only, default: openid email profile)"
        _a TURNSTONE_OIDC_SCOPES
        printf '%s\n' "# ID-token claim holding group/role values (generic only, default: groups)"
        _a TURNSTONE_OIDC_ROLE_CLAIM
        printf '%s\n' "# Claim value / group → Turnstone admin role  (default: admin / turnstone-admins)"
        _a TURNSTONE_SETUP_OIDC_ADMIN_VALUE
        printf '%s\n' "# Claim value / group → general user access  (default: users / turnstone-users)"
        _a TURNSTONE_SETUP_OIDC_USER_VALUE
        printf '%s\n' "# Extra trusted OIDC endpoint hostnames, comma-separated (optional, generic only)"
        _a TURNSTONE_OIDC_TRUSTED_ENDPOINT_HOSTS
        printf '%s\n' "# Public origin browsers use to reach Turnstone  (default: https://localhost:8443)"
        _a TURNSTONE_OIDC_REDIRECT_BASE
        printf '%s\n' "# Does the OIDC provider resolve to a private/internal address?  Values: y | n  (default: n)"
        _a TURNSTONE_SETUP_OIDC_PRIVATE
        printf '%s\n' "# Disable password logins once SSO works (SSO-only mode)?  Values: y | n  (default: n)"
        _a TURNSTONE_SETUP_OIDC_SSO_ONLY
        cat <<'S4'

# ---------------------------------------------------------------------------
# TLS (optional)
# ---------------------------------------------------------------------------
# Enable mutual TLS between Turnstone services?  Values: y | n  (default: n)
S4
        _a TURNSTONE_SETUP_TLS
        printf '%s\n' "# Serve the dashboard at a public DNS name with a browser-trusted certificate?  Values: y | n  (default: n)"
        _a TURNSTONE_SETUP_PUBLIC_DNS
        cat <<'S4A'
# --- Public DNS settings (only when TURNSTONE_SETUP_PUBLIC_DNS=y) ---
# Public DNS hostname (e.g. turnstone.example.com)
S4A
        _a TURNSTONE_SETUP_DOMAIN
        printf '%s\n' "# Email address for Let's Encrypt registration"
        _a TURNSTONE_SETUP_ACME_EMAIL
        printf '%s\n' "# DNS challenge provider.  Values: cloudflare | route53 | duckdns  (default: cloudflare)"
        _a TURNSTONE_SETUP_DNS_PROVIDER
        printf '%s\n' "# Cloudflare DNS API token with Zone:DNS:Edit permission  (cloudflare only)"
        _a CF_DNS_API_TOKEN
        printf '%s\n' "# AWS access key ID for Route 53 DNS challenge  (route53 only)"
        _a TURNSTONE_SETUP_AWS_ACCESS_KEY_ID
        printf '%s\n' "# AWS secret access key for Route 53 DNS challenge  (route53 only)"
        _a TURNSTONE_SETUP_AWS_SECRET_ACCESS_KEY
        printf '%s\n' "# Duck DNS API token  (duckdns only)"
        _a TURNSTONE_SETUP_DUCKDNS_TOKEN
        cat <<'S5'

# ---------------------------------------------------------------------------
# LLM backend (optional)
# ---------------------------------------------------------------------------
# Configure a default LLM backend now?  Values: y | n  (default: n)
# Backends can also be connected later in the console Models tab.
S5
        _a TURNSTONE_SETUP_LLM
        printf '%s\n' "# OpenAI-compatible base URL  (default: http://host.docker.internal:8000/v1)"
        _a LLM_BASE_URL
        printf '%s\n' "# API key for the LLM backend  (default: dummy)"
        _a OPENAI_API_KEY
        printf '%s\n' "# Default model alias (optional, leave blank to skip)"
        _a MODEL
        cat <<'S6'

# ---------------------------------------------------------------------------
# Channel gateway (optional)
# ---------------------------------------------------------------------------
# Connect chat channels (Discord / Slack)?  Values: y | n  (default: n)
S6
        _a TURNSTONE_SETUP_CHANNELS
        printf '%s\n' "# Configure Discord?  Values: y | n  (default: n)"
        _a TURNSTONE_SETUP_DISCORD
        printf '%s\n' "# Discord bot token  (Discord only)"
        _a TURNSTONE_DISCORD_TOKEN
        printf '%s\n' "# Discord guild (server) ID  (Discord only, default: 0)"
        _a TURNSTONE_DISCORD_GUILD
        printf '%s\n' "# Configure Slack?  Values: y | n  (default: n)"
        _a TURNSTONE_SETUP_SLACK
        printf '%s\n' "# Slack bot token starting with xoxb-  (Slack only)"
        _a TURNSTONE_SLACK_TOKEN
        printf '%s\n' "# Slack app-level token starting with xapp-  (Slack only)"
        _a TURNSTONE_SLACK_APP_TOKEN
        cat <<'S7'

# ---------------------------------------------------------------------------
# Secrets (normally generated automatically — only set these to pre-supply
# an existing value, e.g. to pair with an already-initialised data volume)
# ---------------------------------------------------------------------------
# Postgres password.
# Required in non-interactive (AUTORUN=true) mode when the Docker named
# volume turnstone_postgres-data already exists and no .env is present.
# Leave blank to have the script generate a fresh value (or detect it).
S7
        _a POSTGRES_PASSWORD
        } >"$OUT_ANSWERS"
    )
    chmod 600 "$OUT_ANSWERS"
}

# ---------------------------------------------------------------------------
# .gitignore sync: every path this script generates in the working tree,
# appended to the repository-root .gitignore when not already ignored.
# ---------------------------------------------------------------------------
GITIGNORE_ENTRIES=(
    "override.compose.yaml"
    "deploy/auto-deployment/compose.yaml"
    "deploy/auto-deployment/compose.override.yaml"
    "deploy/auto-deployment/tls.compose.yaml"
    "deploy/auto-deployment/Caddyfile"
    "deploy/auto-deployment/caddy/"
    "deploy/auto-deployment/config/"
    "deploy/auto-deployment/searxng/"
    "deploy/auto-deployment/setup-production.env"
    "deploy/auto-deployment/.env"
)

# True when .gitignore already lists the exact pattern (bare or rooted).
_gitignore_has_line() {
    local gitignore="$1" entry="$2" line
    [ -f "$gitignore" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"   # strip leading blanks
        line="${line%"${line##*[![:space:]]}"}"   # strip trailing blanks
        [ -z "$line" ] && continue
        [ "${line:0:1}" = "#" ] && continue
        if [ "$line" = "$entry" ] || [ "$line" = "/$entry" ]; then
            return 0
        fi
    done <"$gitignore"
    return 1
}

ensure_gitignore_entries() {
    local gitignore="$REPO_DIR/.gitignore"
    local -a missing=()
    local entry

    for entry in "${GITIGNORE_ENTRIES[@]}"; do
        _gitignore_has_line "$gitignore" "$entry" && continue
        # Skip entries an existing rule already ignores (e.g. a bare `.env`).
        if have git && git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1 \
            && git -C "$REPO_DIR" check-ignore -q "$entry" 2>/dev/null; then
            continue
        fi
        missing+=("$entry")
    done

    if [ ${#missing[@]} -eq 0 ]; then
        info "Repository .gitignore already covers the generated deployment files."
        return 0
    fi

    if [ -e "$gitignore" ] && [ ! -w "$gitignore" ]; then
        warn "cannot update $gitignore (not writable); add these entries manually: ${missing[*]}"
        return 0
    fi

    info "Adding ${#missing[@]} entry/entries to $gitignore"
    # Command substitution drops a trailing newline: non-empty means the
    # file does not end with one, so start the block with a separator.
    local trailer=""
    if [ -s "$gitignore" ] && [ -n "$(tail -c 1 "$gitignore")" ]; then
        trailer=$'\n'
    fi
    {
        printf '%s\n# Generated by deploy/auto-deployment/setup-production.sh\n' "$trailer"
        printf '%s\n' "${missing[@]}"
    } >>"$gitignore"
}

check_prereqs() {
    have docker || die "docker is required. Install Docker Engine first: https://docs.docker.com/engine/install/"
    docker compose version >/dev/null 2>&1 || die "the Docker Compose v2 plugin is required (docker compose version failed)."
    [ -f "$SRC_COMPOSE" ] || die "missing $SRC_COMPOSE — run this script from a full repository clone."
    [ -f "$SRC_CADDYFILE" ] || die "missing $SRC_CADDYFILE — run this script from a full repository clone."
    [ -d "$SRC_SEARXNG_DIR" ] || die "missing $SRC_SEARXNG_DIR — run this script from a full repository clone."
}

# ---------------------------------------------------------------------------
# Image tag selection: track `latest` or pin a released tag. The pinned path
# resolves the newest release tag from the image source repository, with a
# manual fallback when the lookup is unavailable.
# ---------------------------------------------------------------------------
resolve_pinned_tag() {
    local tag=""
    if have curl; then
        tag="$(curl -fsS --max-time 10 "$IMAGE_REPO_API/releases/latest" 2>/dev/null \
            | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)" || tag=""
        if [ -z "$tag" ]; then
            tag="$(curl -fsS --max-time 10 "$IMAGE_REPO_API/tags?per_page=1" 2>/dev/null \
                | sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)" || tag=""
        fi
    fi
    printf '%s' "$tag"
}

section_image_tag() {
    local channel discovered
    prompt_choice channel TURNSTONE_SETUP_IMAGE_CHANNEL \
        "Track the rolling 'latest' image or pin a released tag?" latest latest pinned
    if [ "$channel" = latest ]; then
        IMAGE_TAG=latest
        return
    fi
    info "Looking up the newest released tag…"
    discovered="$(resolve_pinned_tag)"
    if [ -n "$discovered" ]; then
        prompt_value IMAGE_TAG TURNSTONE_SETUP_IMAGE_TAG "Image tag to pin" "$discovered"
    else
        warn "could not resolve a release tag automatically."
        prompt_value IMAGE_TAG TURNSTONE_SETUP_IMAGE_TAG "Image tag to pin (e.g. v1.2.3)"
    fi
}

# ---------------------------------------------------------------------------
# Server node count. Node 1 is the base compose `server` service; any extra
# node is generated into compose.override.yaml with its own identity.
# ---------------------------------------------------------------------------
section_nodes() {
    while :; do
        prompt_value NODE_COUNT TURNSTONE_SETUP_NODE_COUNT \
            "How many turnstone-server nodes should this deployment run?" "1"
        [[ "$NODE_COUNT" =~ ^[1-9][0-9]*$ ]] && return
        # Re-prompting cannot fix a bad exported/non-interactive value.
        if [[ -v TURNSTONE_SETUP_NODE_COUNT ]] || _no_tty; then
            die "node count must be a positive integer (got '$NODE_COUNT')"
        fi
        _FILE_DEFAULTS[TURNSTONE_SETUP_NODE_COUNT]=""
        printf 'Enter a positive whole number.\n' >/dev/tty
    done
}

# ---------------------------------------------------------------------------
# OIDC single sign-on. The Authentik path asks only for what an Authentik
# provider needs; role claims map an admin group and a general-users group
# onto the built-in Turnstone roles.
# ---------------------------------------------------------------------------
section_oidc() {
    OIDC_ENABLED=0
    ask "Enable OIDC single sign-on?" TURNSTONE_SETUP_OIDC n || return 0
    OIDC_ENABLED=1

    local provider
    prompt_choice provider TURNSTONE_SETUP_OIDC_PROVIDER \
        "Identity provider type" generic authentik generic

    if [ "$provider" = authentik ]; then
        local ak_base ak_slug
        info "Register Turnstone in Authentik as a confidential OAuth2/OpenID provider first."
        info "Redirect URI to register: <public origin>/v1/api/auth/oidc/callback"
        prompt_value ak_base TURNSTONE_SETUP_AUTHENTIK_URL "Authentik base URL (e.g. https://authentik.example.com)"
        ak_base="${ak_base%/}"
        prompt_value ak_slug TURNSTONE_SETUP_AUTHENTIK_SLUG "Authentik application slug"
        OIDC_ISSUER="$ak_base/application/o/$ak_slug/"
        OIDC_PROVIDER_NAME="Authentik"
        OIDC_SCOPES="openid email profile"
        # Authentik exposes group membership through the `groups` claim.
        OIDC_ROLE_CLAIM="groups"
        prompt_value OIDC_CLIENT_ID TURNSTONE_OIDC_CLIENT_ID "Authentik client ID"
        prompt_value OIDC_CLIENT_SECRET TURNSTONE_OIDC_CLIENT_SECRET "Authentik client secret"
        local admin_group user_group
        prompt_value admin_group TURNSTONE_SETUP_OIDC_ADMIN_VALUE "Authentik group granted the Turnstone admin role" "turnstone-admins"
        prompt_value user_group TURNSTONE_SETUP_OIDC_USER_VALUE "Authentik group granted general user access" "turnstone-users"
        OIDC_ROLE_MAP="$admin_group:builtin-admin,$user_group:builtin-operator"
        OIDC_TRUSTED_HOSTS=""
    else
        prompt_value OIDC_ISSUER TURNSTONE_OIDC_ISSUER "OIDC issuer URL (must serve /.well-known/openid-configuration)"
        prompt_value OIDC_CLIENT_ID TURNSTONE_OIDC_CLIENT_ID "OIDC client ID"
        prompt_value OIDC_CLIENT_SECRET TURNSTONE_OIDC_CLIENT_SECRET "OIDC client secret"
        prompt_value OIDC_PROVIDER_NAME TURNSTONE_OIDC_PROVIDER_NAME "Login button label" "SSO"
        prompt_value OIDC_SCOPES TURNSTONE_OIDC_SCOPES "OAuth scopes" "openid email profile"
        prompt_value OIDC_ROLE_CLAIM TURNSTONE_OIDC_ROLE_CLAIM "ID-token claim holding group/role values" "groups"
        local admin_value user_value
        prompt_value admin_value TURNSTONE_SETUP_OIDC_ADMIN_VALUE "Claim value granted the Turnstone admin role" "admin"
        prompt_value user_value TURNSTONE_SETUP_OIDC_USER_VALUE "Claim value granted general user access" "users"
        OIDC_ROLE_MAP="$admin_value:builtin-admin,$user_value:builtin-operator"
        prompt_optional OIDC_TRUSTED_HOSTS TURNSTONE_OIDC_TRUSTED_ENDPOINT_HOSTS \
            "Extra trusted endpoint hostnames (comma-separated)"
    fi

    prompt_value OIDC_REDIRECT_BASE TURNSTONE_OIDC_REDIRECT_BASE \
        "Public origin browsers use to reach Turnstone (e.g. https://turnstone.example.com)" \
        "https://localhost:8443"
    OIDC_REDIRECT_BASE="${OIDC_REDIRECT_BASE%/}"

    OIDC_ALLOW_PRIVATE=false
    if ask "Does the identity provider resolve to a private/internal address?" TURNSTONE_SETUP_OIDC_PRIVATE n; then
        OIDC_ALLOW_PRIVATE=true
    fi
    OIDC_PASSWORD_ENABLED=true
    if ask "Disable password logins once SSO works (SSO-only mode)?" TURNSTONE_SETUP_OIDC_SSO_ONLY n; then
        OIDC_PASSWORD_ENABLED=false
    fi
}

# ---------------------------------------------------------------------------
# TLS: service-to-service mTLS overlay, then optional public HTTPS where
# Caddy obtains a Let's Encrypt certificate through a DNS-01 challenge.
# ---------------------------------------------------------------------------
section_tls() {
    MTLS_ENABLED=0
    PUBLIC_DNS_ENABLED=0
    DOMAIN=""
    DNS_PROVIDER=""
    ACME_EMAIL=""
    CF_TOKEN=""
    AWS_KEY_ID=""
    AWS_SECRET=""
    DUCKDNS_TOKEN_VAL=""

    if ask "Enable TLS (mutual TLS between Turnstone services)?" TURNSTONE_SETUP_TLS n; then
        MTLS_ENABLED=1
    fi

    ask "Serve the dashboard on a public DNS name with a browser-trusted certificate?" TURNSTONE_SETUP_PUBLIC_DNS n || return 0
    PUBLIC_DNS_ENABLED=1

    prompt_value DOMAIN TURNSTONE_SETUP_DOMAIN "Public DNS name Turnstone should be reachable at (e.g. turnstone.example.com)"
    prompt_value ACME_EMAIL TURNSTONE_SETUP_ACME_EMAIL "Email address for Let's Encrypt registration"
    prompt_choice DNS_PROVIDER TURNSTONE_SETUP_DNS_PROVIDER \
        "DNS challenge provider" cloudflare cloudflare route53 duckdns

    case "$DNS_PROVIDER" in
        cloudflare)
            prompt_value CF_TOKEN CF_DNS_API_TOKEN "Cloudflare DNS API token (Zone:DNS:Edit for the domain)"
            ;;
        route53)
            prompt_value AWS_KEY_ID TURNSTONE_SETUP_AWS_ACCESS_KEY_ID "AWS access key ID (Route 53 change-record permissions)"
            prompt_value AWS_SECRET TURNSTONE_SETUP_AWS_SECRET_ACCESS_KEY "AWS secret access key"
            ;;
        duckdns)
            prompt_value DUCKDNS_TOKEN_VAL TURNSTONE_SETUP_DUCKDNS_TOKEN "Duck DNS API token"
            ;;
    esac

    # The public-DNS path builds a local Caddy image with the DNS plugin.
    # Compose builds with buildx when available; without it, it falls back
    # to the deprecated classic builder (slower, prints a WARN, and will be
    # removed in a future Docker release).
    if ! docker buildx version >/dev/null 2>&1; then
        warn "the Docker buildx plugin is not installed; 'docker compose up' will fall back to the legacy builder to build the Caddy DNS image. Install docker-buildx-plugin to silence the compose WARN: https://docs.docker.com/build/install-buildx/"
    fi
}

# ---------------------------------------------------------------------------
# LLM backend bootstrap defaults. Real backends can also be connected later
# from the console Models tab.
# ---------------------------------------------------------------------------
section_llm() {
    LLM_ENABLED=0
    ask "Configure a default LLM backend now?" TURNSTONE_SETUP_LLM n || return 0
    LLM_ENABLED=1
    prompt_value LLM_BASE_URL_VAL LLM_BASE_URL "OpenAI-compatible base URL" "http://host.docker.internal:8000/v1"
    prompt_value LLM_API_KEY OPENAI_API_KEY "API key for the backend" "dummy"
    prompt_optional LLM_MODEL MODEL "Default model alias"
}

# ---------------------------------------------------------------------------
# Channel gateway (Discord and/or Slack). Runs idle when no token is set.
# ---------------------------------------------------------------------------
section_channels() {
    CHANNELS_ENABLED=0
    DISCORD_TOKEN=""
    DISCORD_GUILD="0"
    SLACK_TOKEN=""
    SLACK_APP_TOKEN=""
    ask "Connect chat channels (Discord / Slack)?" TURNSTONE_SETUP_CHANNELS n || return 0
    CHANNELS_ENABLED=1
    if ask "Configure Discord?" TURNSTONE_SETUP_DISCORD n; then
        prompt_value DISCORD_TOKEN TURNSTONE_DISCORD_TOKEN "Discord bot token"
        prompt_value DISCORD_GUILD TURNSTONE_DISCORD_GUILD "Discord guild (server) ID" "0"
    fi
    if ask "Configure Slack?" TURNSTONE_SETUP_SLACK n; then
        prompt_value SLACK_TOKEN TURNSTONE_SLACK_TOKEN "Slack bot token (xoxb-…)"
        prompt_value SLACK_APP_TOKEN TURNSTONE_SLACK_APP_TOKEN "Slack app-level token (xapp-…)"
    fi
}

# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------
copy_stack_files() {
    info "Copying the production stack files into $SCRIPT_DIR"
    cp "$SRC_COMPOSE" "$OUT_COMPOSE"
    rm -rf "$OUT_SEARXNG_DIR"
    mkdir -p "$OUT_SEARXNG_DIR"
    cp "$SRC_SEARXNG_DIR"/* "$OUT_SEARXNG_DIR/"
    if [ "$MTLS_ENABLED" = 1 ]; then
        cp "$SRC_TLS_OVERLAY" "$OUT_TLS_OVERLAY"
        sanitize_tls_overlay
    else
        rm -f "$OUT_TLS_OVERLAY"
    fi
}

# The upstream TLS overlay overrides the console service command with a
# `--poll-interval` flag that the current turnstone-console CLI no longer
# accepts, which crash-loops the console ("unrecognized arguments").
# Dropping the whole `command:` override is safe: without it the console
# falls back to the base compose command (turnstone-console
# --host=0.0.0.0 --port=8090), which is exactly what the override ran.
#
# The removal is structure-aware (YAML indentation based), not tied to
# line numbers or file order, so it keeps working if the upstream file
# is reordered, reformatted, or grows/shrinks. It only touches the
# `command:` key of the `console:` service under `services:` — every
# other service (e.g. tls-init, channel) keeps its command untouched.
sanitize_tls_overlay() {
    [ -f "$OUT_TLS_OVERLAY" ] || die "TLS overlay $OUT_TLS_OVERLAY is missing; the copy from $SRC_TLS_OVERLAY failed"
    local tmp
    tmp="$(mktemp)" || die "mktemp failed while sanitizing $OUT_TLS_OVERLAY"
    if ! awk '
        BEGIN { in_services = 0; in_console = 0; skipping = 0; blanks = 0 }
        function indent_of(line,    n) {
            n = 0
            while (substr(line, n + 1, 1) == " ") n++
            return n
        }
        {
            line = $0
            trimmed = line
            sub(/^[ \t]+/, "", trimmed)
            is_blank = (trimmed == "")
            is_comment = (substr(trimmed, 1, 1) == "#")
            is_content = (!is_blank && !is_comment)
            ind = indent_of(line)

            # While removing the console command block: swallow every line
            # indented deeper than the `command:` key. Blank lines are held
            # back until we know whether the block continues after them.
            if (skipping) {
                if (is_blank) { blanks++; next }
                if (ind > cmd_indent) { blanks = 0; next }
                skipping = 0
                while (blanks > 0) { print ""; blanks-- }
            }

            # Leave the console service / services section when a content
            # line appears at the same or shallower indentation.
            if (in_console && is_content && ind <= console_indent) in_console = 0
            if (in_services && is_content && ind <= services_indent) in_services = 0

            if (is_content && ind == 0 && trimmed ~ /^services:[ \t]*(#.*)?$/) {
                in_services = 1
                services_indent = ind
            } else if (in_services && !in_console && is_content && trimmed ~ /^console:[ \t]*(#.*)?$/) {
                in_console = 1
                console_indent = ind
            } else if (in_console && is_content && trimmed ~ /^command:([ \t].*)?$/) {
                skipping = 1
                cmd_indent = ind
                blanks = 0
                next
            }
            print
        }
    ' "$OUT_TLS_OVERLAY" >"$tmp"; then
        rm -f "$tmp"
        die "Failed to sanitize $OUT_TLS_OVERLAY"
    fi
    mv "$tmp" "$OUT_TLS_OVERLAY"
    # Belt and braces: the removed override carried the unsupported
    # --poll-interval flag. If it is still present the console would
    # crash-loop, so fail loudly rather than deploy a broken stack.
    if grep -q -- '--poll-interval' "$OUT_TLS_OVERLAY"; then
        die "Sanitizing $OUT_TLS_OVERLAY failed: unsupported --poll-interval flag is still present"
    fi
}

write_caddyfile() {
    if [ "$PUBLIC_DNS_ENABLED" != 1 ]; then
        cp "$SRC_CADDYFILE" "$OUT_CADDYFILE"
        rm -rf "$OUT_CADDY_DIR"
        return
    fi

    local tls_line
    case "$DNS_PROVIDER" in
        cloudflare) tls_line="dns cloudflare {env.CF_DNS_API_TOKEN}" ;;
        route53)    tls_line="dns route53" ;;
        duckdns)    tls_line="dns duckdns {env.DUCKDNS_API_TOKEN}" ;;
    esac

    # Public HTTPS: Let's Encrypt certificate via DNS-01, dashboard on 443.
    cat >"$OUT_CADDYFILE" <<EOF
{
	email $ACME_EMAIL
}

$DOMAIN {
	tls {
		$tls_line
	}
	reverse_proxy console:8090 {
		flush_interval -1
	}
}
EOF

    # The stock Caddy image ships no DNS plugins; build one in.
    mkdir -p "$OUT_CADDY_DIR"
    cat >"$OUT_CADDY_DIR/Dockerfile" <<EOF
FROM caddy:2.11-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/$DNS_PROVIDER
FROM caddy:2.11
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
EOF
}

write_oidc_env() {
    if [ "$OIDC_ENABLED" != 1 ]; then
        rm -f "$OUT_OIDC_ENV"
        return
    fi
    mkdir -p "$OUT_CONFIG_DIR"
    cat >"$OUT_OIDC_ENV" <<EOF
TURNSTONE_OIDC_ISSUER=$OIDC_ISSUER
TURNSTONE_OIDC_CLIENT_ID=$OIDC_CLIENT_ID
TURNSTONE_OIDC_CLIENT_SECRET=$OIDC_CLIENT_SECRET
TURNSTONE_OIDC_SCOPES=$OIDC_SCOPES
TURNSTONE_OIDC_PROVIDER_NAME=$OIDC_PROVIDER_NAME
TURNSTONE_OIDC_ROLE_CLAIM=$OIDC_ROLE_CLAIM
TURNSTONE_OIDC_ROLE_MAP=$OIDC_ROLE_MAP
TURNSTONE_OIDC_REDIRECT_BASE=$OIDC_REDIRECT_BASE
TURNSTONE_OIDC_PASSWORD_ENABLED=$OIDC_PASSWORD_ENABLED
TURNSTONE_OIDC_ALLOW_PRIVATE_NETWORK=$OIDC_ALLOW_PRIVATE
TURNSTONE_OIDC_TRUSTED_ENDPOINT_HOSTS=$OIDC_TRUSTED_HOSTS
EOF
    chmod 600 "$OUT_OIDC_ENV"
}

# Nodes 2…N. Each extends the base `server` service and overrides only its
# own identity. `extends` does not carry depends_on, so it is restated here.
write_extra_nodes() {
    local i
    for ((i = 2; i <= NODE_COUNT; i++)); do
        cat <<EOF
  server-$i:
    extends:
      file: compose.yaml
      service: server
    environment:
      TURNSTONE_NODE_ID: node-$i
      TURNSTONE_ADVERTISE_URL: http://server-$i:8080
EOF
        if [ "$MTLS_ENABLED" = 1 ]; then
            cat <<EOF
      TURNSTONE_TLS_ENABLED: "true"
      TURNSTONE_TLS_SANS: server-$i
EOF
        fi
        if [ "$OIDC_ENABLED" = 1 ]; then
            cat <<'EOF'
    env_file:
      - ./config/turnstone-oidc.env
EOF
        fi
        cat <<'EOF'
    depends_on:
      postgres:
        condition: service_healthy
      searxng:
        condition: service_healthy
EOF
        if [ "$MTLS_ENABLED" = 1 ]; then
            # Mirrors tls.compose.yaml's `server` patch: the node enrolls its
            # cert through the console and then only speaks mTLS, which the
            # plain-HTTP healthcheck cannot probe.
            cat <<'EOF'
      console:
        condition: service_healthy
    volumes:
      - tls-certs:/certs:ro
    healthcheck:
      disable: true
EOF
        fi
    done
}

write_override() {
    local need_override=0
    if [ "$OIDC_ENABLED" = 1 ]; then need_override=1; fi
    if [ "$PUBLIC_DNS_ENABLED" = 1 ]; then need_override=1; fi
    if [ "$NODE_COUNT" -gt 1 ]; then need_override=1; fi
    if [ "$need_override" != 1 ]; then
        rm -f "$OUT_OVERRIDE"
        return
    fi

    {
        echo "# Deployment-specific adjustments layered over compose.yaml."
        echo "services:"
        if [ "$OIDC_ENABLED" = 1 ]; then
            for svc in console server; do
                cat <<EOF
  $svc:
    env_file:
      - ./config/turnstone-oidc.env
EOF
            done
        fi
        if [ "$NODE_COUNT" -gt 1 ]; then
            write_extra_nodes
        fi
        if [ "$PUBLIC_DNS_ENABLED" = 1 ]; then
            # pull_policy: build — the image only exists locally (built from
            # ./caddy). Without it, `docker compose up` first tries to pull
            # the tag from a registry and prints a confusing
            # "pull access denied for turnstone-caddy" error.
            cat <<'EOF'
  caddy:
    build: ./caddy
    image: turnstone-caddy:dns
    pull_policy: build
    ports:
      - "80:80"
EOF
            case "$DNS_PROVIDER" in
                cloudflare)
                    printf '    environment:\n      CF_DNS_API_TOKEN: ${CF_DNS_API_TOKEN}\n' ;;
                route53)
                    printf '    environment:\n      AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID}\n      AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY}\n' ;;
                duckdns)
                    printf '    environment:\n      DUCKDNS_API_TOKEN: ${DUCKDNS_API_TOKEN}\n' ;;
            esac
        fi
    } >"$OUT_OVERRIDE"
}

write_env_file() {
    # Compose auto-loads compose.override.yaml, but only while COMPOSE_FILE is
    # unset — so the variable is written for the mTLS chain only, and must then
    # name the override explicitly.
    local compose_chain=""
    if [ "$MTLS_ENABLED" = 1 ]; then
        compose_chain="compose.yaml"
        if [ -f "$OUT_OVERRIDE" ]; then compose_chain="$compose_chain:compose.override.yaml"; fi
        compose_chain="$compose_chain:tls.compose.yaml"
    fi

    info "Generating secrets and writing $OUT_ENV"
    local jwt_secret pg_password existing_jwt="" existing_pg=""

    # Preserve existing secrets from a prior .env so a rerun does not
    # regenerate them and break an existing Postgres data volume.
    if [ -f "$OUT_ENV" ]; then
        existing_jwt="$(grep -m1 '^TURNSTONE_JWT_SECRET=' "$OUT_ENV" | cut -d= -f2-)" || true
        existing_pg="$(grep -m1 '^POSTGRES_PASSWORD=' "$OUT_ENV" | cut -d= -f2-)" || true
    fi

    jwt_secret="${TURNSTONE_JWT_SECRET:-${existing_jwt:-$(gen_hex)}}"

    # Resolve POSTGRES_PASSWORD. When a fresh random value would be generated
    # (no exported variable and no existing .env value), check for a stale
    # Docker named volume that was initialised with a different password —
    # Postgres only applies POSTGRES_PASSWORD on first data-directory init.
    local _pg_source=""
    if [ -n "${POSTGRES_PASSWORD:-}" ]; then
        pg_password="$POSTGRES_PASSWORD"
        _pg_source="env"
    elif [ -n "${_FILE_DEFAULTS[POSTGRES_PASSWORD]:-}" ]; then
        pg_password="${_FILE_DEFAULTS[POSTGRES_PASSWORD]}"
        _ANSWERS[POSTGRES_PASSWORD]="$pg_password"
        _pg_source="file"
    elif [ -n "$existing_pg" ]; then
        pg_password="$existing_pg"
        _pg_source="existing"
    else
        local _pg_vol="${COMPOSE_PROJECT_NAME:-turnstone}_postgres-data"
        local _vol_inspect_out
        if _vol_inspect_out="$(docker volume inspect "$_pg_vol" 2>&1)"; then
            # Volume exists — a freshly generated password would not match the
            # one used when the volume was initialised, breaking auth.
            if _no_tty; then
                die "Postgres data volume '$_pg_vol' already exists but no POSTGRES_PASSWORD is \
available. A newly-generated password will not match the one used when the volume \
was initialised, causing 'FATAL: password authentication failed for user \"turnstone\"'. \
Remedies — choose one and rerun: \
(1) export POSTGRES_PASSWORD=<existing-password> before running the script, \
(2) add POSTGRES_PASSWORD=<existing-password> to $OUT_ANSWERS, or \
(3) remove the stale volume first (destroys all data): docker volume rm $_pg_vol"
            else
                warn "Postgres data volume '$_pg_vol' already exists."
                {
                    printf '\n'
                    printf '  Generating a fresh random POSTGRES_PASSWORD would not match the\n'
                    printf '  password used to initialise the existing data directory, causing:\n'
                    printf '    FATAL: password authentication failed for user "turnstone"\n'
                    printf '\n'
                    printf '  How to proceed:\n'
                    printf '    enter  — supply the existing POSTGRES_PASSWORD to reuse this volume\n'
                    printf '    wipe   — remove the volume and all its data, generate a new password\n'
                    printf '    abort  — exit; handle this manually\n'
                    printf '\n'
                } >/dev/tty
                local _pg_action
                prompt_choice _pg_action POSTGRES_VOLUME_ACTION \
                    "Action for existing volume '$_pg_vol'" enter \
                    enter wipe abort
                case "$_pg_action" in
                    enter)
                        prompt_value pg_password POSTGRES_PASSWORD \
                            "Existing POSTGRES_PASSWORD (will be stored in $OUT_ENV)"
                        _pg_source="entered"
                        ;;
                    wipe)
                        warn "Volume '$_pg_vol' and ALL its data will be permanently deleted."
                        if ! ask "Confirm deletion of volume '$_pg_vol'?" "" n; then
                            die "Deletion not confirmed. Aborting."
                        fi
                        docker volume rm "$_pg_vol" \
                            || die "Could not remove '$_pg_vol'. Steps to resolve: (1) run 'docker compose down' to stop any containers using the volume, (2) verify Docker daemon permissions, (3) rerun this script."
                        info "Volume '$_pg_vol' removed. A fresh password will be generated."
                        pg_password="$(gen_hex)"
                        _pg_source="generated"
                        ;;
                    abort)
                        die "Aborting. To proceed manually: (1) export POSTGRES_PASSWORD=<existing-password> and rerun, or (2) docker volume rm $_pg_vol to start fresh."
                        ;;
                esac
            fi
        else
            # Distinguish "volume not found" from a real Docker error.
            if echo "$_vol_inspect_out" | grep -qi "no such volume\|not found"; then
                pg_password="$(gen_hex)"
                _pg_source="generated"
            else
                # Unexpected Docker error; warn and proceed with a fresh
                # password. If a stale volume exists this may still cause an
                # auth failure, but the user will see the docker error output.
                warn "Could not query Docker volume '$_pg_vol': $_vol_inspect_out"
                warn "Proceeding with a fresh random password. If '$_pg_vol' exists with a different password, 'docker compose up -d' may fail with an auth error — rerun this script and supply POSTGRES_PASSWORD."
                pg_password="$(gen_hex)"
                _pg_source="generated"
            fi
        fi
    fi

    if [ -n "${TURNSTONE_JWT_SECRET:-}" ]; then
        info "TURNSTONE_JWT_SECRET: using exported environment variable"
    elif [ -n "$existing_jwt" ]; then
        warn "TURNSTONE_JWT_SECRET: preserving existing value from $OUT_ENV (not regenerated)"
    else
        info "TURNSTONE_JWT_SECRET: generated new random value"
    fi
    case "$_pg_source" in
        env)      info "POSTGRES_PASSWORD: using exported environment variable" ;;
        file)     info "POSTGRES_PASSWORD: using value from $OUT_ANSWERS" ;;
        existing) warn "POSTGRES_PASSWORD: preserving existing value from $OUT_ENV (not regenerated)" ;;
        entered)  info "POSTGRES_PASSWORD: using entered existing password (volume reused)" ;;
        *)        info "POSTGRES_PASSWORD: generated new random value" ;;
    esac

    {
        echo "# Generated deployment settings. Keep this file private (contains secrets)."
        if [ -n "$compose_chain" ]; then
            echo "COMPOSE_FILE=$compose_chain"
        fi
        echo "TURNSTONE_IMAGE_TAG=$IMAGE_TAG"
        echo "TURNSTONE_JWT_SECRET=$jwt_secret"
        echo "POSTGRES_PASSWORD=$pg_password"
        if [ "$PUBLIC_DNS_ENABLED" = 1 ]; then
            # Publishes caddy on the standard HTTPS port (base maps $CONSOLE_HTTPS_PORT:443).
            echo "CONSOLE_HTTPS_PORT=443"
            case "$DNS_PROVIDER" in
                cloudflare) echo "CF_DNS_API_TOKEN=$CF_TOKEN" ;;
                route53)
                    echo "AWS_ACCESS_KEY_ID=$AWS_KEY_ID"
                    echo "AWS_SECRET_ACCESS_KEY=$AWS_SECRET"
                    ;;
                duckdns) echo "DUCKDNS_API_TOKEN=$DUCKDNS_TOKEN_VAL" ;;
            esac
        fi
        if [ "$LLM_ENABLED" = 1 ]; then
            echo "LLM_BASE_URL=$LLM_BASE_URL_VAL"
            echo "OPENAI_API_KEY=$LLM_API_KEY"
            if [ -n "$LLM_MODEL" ]; then echo "MODEL=$LLM_MODEL"; fi
        fi
        if [ "$CHANNELS_ENABLED" = 1 ]; then
            if [ -n "$DISCORD_TOKEN" ]; then
                echo "TURNSTONE_DISCORD_TOKEN=$DISCORD_TOKEN"
                echo "TURNSTONE_DISCORD_GUILD=$DISCORD_GUILD"
            fi
            if [ -n "$SLACK_TOKEN" ]; then echo "TURNSTONE_SLACK_TOKEN=$SLACK_TOKEN"; fi
            if [ -n "$SLACK_APP_TOKEN" ]; then echo "TURNSTONE_SLACK_APP_TOKEN=$SLACK_APP_TOKEN"; fi
        fi
    } >"$OUT_ENV"
    chmod 600 "$OUT_ENV"
}

print_done() {
    info "Setup complete. Deployment files are in: $SCRIPT_DIR"
    echo
    echo "Start the stack:"
    echo "  cd $SCRIPT_DIR"
    echo "  docker compose up -d"
    echo
    if [ "$PUBLIC_DNS_ENABLED" = 1 ]; then
        echo "Dashboard: https://$DOMAIN (ports 80/443 must be reachable and the"
        echo "DNS record for $DOMAIN must point at this host)."
    else
        echo "Dashboard: https://localhost:8443 (Caddy local CA — trust its root once:"
        echo "  docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt)"
    fi
    echo
    echo "First visit creates the local admin account. Connect an LLM backend in the"
    echo "console Models tab. See deployment.md for post-deploy steps."
}

main() {
    info "Turnstone deployment setup"
    echo

    # First action: make sure the repository ignores everything this script
    # generates, so a fork stays clean and can pull upstream without rebasing.
    ensure_gitignore_entries

    # Load the persistent answers file before any prompts so that prior
    # answers serve as defaults and AUTORUN=true skips all interactive input.
    load_answers_file

    # Split point: a development stack needs none of the questions below.
    local mode
    prompt_choice mode TURNSTONE_SETUP_MODE \
        "Deploy a production environment or a development environment?" production production development
    if [ "$mode" = development ]; then
        info "Development selected — run ./run.sh from the repository root to start the dev stack."
        exit 0
    fi

    check_prereqs

    # Required for any safe deployment: image provenance. Secrets are always
    # generated automatically, so answering 'no' everywhere below still
    # yields a secure stack (local-CA HTTPS, random credentials).
    section_image_tag
    section_nodes

    # Optional feature sections — each starts with a yes/no.
    section_oidc
    section_tls
    section_llm
    section_channels

    copy_stack_files
    write_caddyfile
    write_oidc_env
    write_override
    write_env_file
    write_answers_file
    print_done
}

main "$@"
