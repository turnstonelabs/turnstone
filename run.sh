#!/usr/bin/env bash
#
# Turnstone one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/turnstonelabs/turnstone/main/run.sh | bash
#
# Autodetects your distro — Ubuntu/Debian, Fedora/RHEL, Arch, their common
# derivatives (Mint, Pop!_OS, Nobara, AlmaLinux, …), and WSL on any of them —
# and:
#   1. ensures git is installed, then clones the repo
#   2. ensures Docker + the compose plugin are installed and the daemon is usable
#   3. asks how many server nodes to run (1-10)
#   4. builds the image
#   5. picks free host ports for Caddy (prefers 443) and PostgreSQL
#   6. writes a .env with a generated JWT secret + Postgres password
#   7. optionally creates shared config.toml for OAuth/SSO and delegation
#   8. pins the choices and runs `docker compose up -d`, then prints how to
#      finish setup in the UI
#
# Re-running is safe: it updates the checkout and keeps existing .env/config.toml.
#
# Env overrides:
#   TURNSTONE_DIR     where to clone (default: $HOME/turnstone)
#   TURNSTONE_REPO    git URL (default: https://github.com/turnstonelabs/turnstone.git)
#   TURNSTONE_BRANCH  git branch to clone (default: main, the stable source)

set -Eeuo pipefail

REPO_URL="${TURNSTONE_REPO:-https://github.com/turnstonelabs/turnstone.git}"
INSTALL_DIR="${TURNSTONE_DIR:-$HOME/turnstone}"
SOURCE_BRANCH="${TURNSTONE_BRANCH:-main}"

# -- output helpers -----------------------------------------------------------
if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
info() { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Any *unhandled* failure (set -Ee) lands here with an actionable note instead of
# a bare non-zero exit. `die` exits explicitly and does not trigger this.
on_error() {
    local rc=$?
    printf '\n%serror:%s the installer stopped unexpectedly (exit %s). The output above shows why.\n' \
        "$RED" "$RESET" "$rc" >&2
    printf '       Fix the issue and re-run this script — it resumes the parts already done.\n' >&2
}
trap on_error ERR

# Ask a yes/no question, reading from the terminal even under `curl | bash`
# (where stdin is the script). Defaults to "$2" when non-interactive.
ask() {
    local prompt="$1" default="${2:-y}" ans hint
    [ "$default" = y ] && hint="Y/n" || hint="y/N"
    if ! ( : </dev/tty ) 2>/dev/null; then
        warn "non-interactive shell; assuming '$default' for: $prompt"
        [ "$default" = y ]; return
    fi
    printf '%s%s%s [%s] ' "$BOLD" "$prompt" "$RESET" "$hint" >/dev/tty
    read -r ans </dev/tty || ans=""
    ans="${ans:-$default}"
    case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# -- distro / package manager detection --------------------------------------
OS_ID=""; OS_LIKE=""; PKG=""; IS_WSL=0; SUDO=""
# Extra os-release fields, captured only to pick Docker's upstream repo when
# get.docker.com refuses a derivative it doesn't recognize (see install_docker).
OS_PLATFORM_ID=""; OS_CODENAME=""; OS_UBUNTU_CODENAME=""

detect_os() {
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        OS_ID="${ID:-}"; OS_LIKE="${ID_LIKE:-}"
        OS_PLATFORM_ID="${PLATFORM_ID:-}"
        OS_CODENAME="${VERSION_CODENAME:-}"
        OS_UBUNTU_CODENAME="${UBUNTU_CODENAME:-}"
    fi
    if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null || [ -n "${WSL_DISTRO_NAME:-}" ]; then
        IS_WSL=1
    fi
    case " $OS_ID $OS_LIKE " in
        *" arch "*|*manjaro*)                 PKG=pacman ;;
        *" ubuntu "*|*" debian "*)            PKG=apt ;;
        *" fedora "*|*" rhel "*|*" centos "*) PKG=dnf ;;
        *)  if   have apt-get; then PKG=apt
            elif have dnf;     then PKG=dnf
            elif have yum;     then PKG=yum
            elif have pacman;  then PKG=pacman
            fi ;;
    esac
    [ -n "$PKG" ] || die "could not detect a supported package manager (apt/dnf/yum/pacman). Install git + Docker manually, then re-run."
    [ "$PKG" = dnf ] && ! have dnf && have yum && PKG=yum
    if [ "$(id -u)" -ne 0 ]; then
        have sudo || die "this script needs root for package installs — install sudo or run as root."
        SUDO="sudo"
    fi
    local where="$OS_ID"; [ "$IS_WSL" -eq 1 ] && where="$OS_ID (WSL)"
    info "Detected ${where:-linux} — using ${PKG}."
}

pkg_install() {
    info "Installing: $*"
    case "$PKG" in
        apt)    $SUDO apt-get update -y && $SUDO apt-get install -y "$@" ;;
        dnf)    $SUDO dnf install -y "$@" ;;
        yum)    $SUDO yum install -y "$@" ;;
        pacman) $SUDO pacman -Sy --needed --noconfirm "$@" ;;
    esac
}

# -- git ----------------------------------------------------------------------
ensure_git() {
    have git && return
    warn "git is not installed."
    ask "Install git now?" y || die "git is required to clone the repo."
    pkg_install git || die "git installation failed — see the output above."
    have git || die "git installation reported success but 'git' is not on PATH."
}

clone_repo() {
    if [ -d "$INSTALL_DIR/.git" ]; then
        info "Updating existing checkout at $INSTALL_DIR"
        local current_branch
        current_branch=$(git -C "$INSTALL_DIR" symbolic-ref --quiet --short HEAD || true)
        if [ -n "$current_branch" ] && [ "$current_branch" != "$SOURCE_BRANCH" ]; then
            warn "existing checkout is on '$current_branch', not configured branch '$SOURCE_BRANCH'; leaving its branch unchanged."
        fi
        git -C "$INSTALL_DIR" pull --ff-only || warn "could not fast-forward; using the existing checkout."
    else
        [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ] \
            && die "$INSTALL_DIR exists and is not a turnstone checkout. Set TURNSTONE_DIR to an empty path."
        info "Cloning $REPO_URL branch $SOURCE_BRANCH into $INSTALL_DIR"
        # Skip Git LFS smudge — the LFS objects are only diagram PNGs, not needed to run.
        GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 --branch "$SOURCE_BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
}

# -- docker -------------------------------------------------------------------
DOCKER="docker"

# Fallback when get.docker.com won't install here. That script keys off $ID alone
# (never ID_LIKE), so it aborts with "Unsupported distribution '<id>'" on every
# derivative — Nobara, Linux Mint, Pop!_OS, AlmaLinux, Oracle Linux, … — even
# though the family is clear. We already know the family from detect_os, so we add
# Docker's official CE repo for the matching upstream and install the same
# packages get.docker.com would (including the compose plugin the rest of run.sh
# relies on).
install_docker_ce_repo() {
    local up
    case "$PKG" in
        apt)
            local codename arch
            # UBUNTU_CODENAME is set by Ubuntu and every Ubuntu-derived distro
            # (Mint/Pop!_OS/Zorin/…) and never by pure Debian, so it both routes
            # the family and gives the exact codename Docker's repo expects.
            if [ -n "$OS_UBUNTU_CODENAME" ]; then
                up=ubuntu; codename="$OS_UBUNTU_CODENAME"
            else
                up=debian; codename="$OS_CODENAME"
            fi
            [ -n "$codename" ] || die "couldn't determine the $up release codename for Docker's repo — install Docker manually and re-run."
            arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
            info "Adding Docker's $up repository ($codename)."
            $SUDO install -m 0755 -d /etc/apt/keyrings
            curl -fsSL "https://download.docker.com/linux/$up/gpg" | $SUDO tee /etc/apt/keyrings/docker.asc >/dev/null
            $SUDO chmod a+r /etc/apt/keyrings/docker.asc
            printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
                "$arch" "$up" "$codename" | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
            $SUDO apt-get update -y
            $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            ;;
        dnf|yum)
            # A Fedora spin and a RHEL clone can both carry "fedora" in ID_LIKE
            # (Nobara's is "rhel centos fedora"), so ID_LIKE can't separate them.
            # PLATFORM_ID can: Fedora is platform:fNN, Enterprise Linux platform:elN.
            case "$OS_PLATFORM_ID" in
                platform:f*)  up=fedora ;;
                platform:el*) up=centos ;;
                *) if [ -e /etc/fedora-release ]; then up=fedora; else up=centos; fi ;;
            esac
            info "Adding Docker's $up repository."
            $SUDO curl -fsSL "https://download.docker.com/linux/$up/docker-ce.repo" \
                -o /etc/yum.repos.d/docker-ce.repo \
                || die "couldn't add Docker's $up repository — install Docker manually and re-run."
            pkg_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            ;;
    esac
}

# The distro IDs get.docker.com installs directly: it matches $ID against this
# exact set (ignoring ID_LIKE) and aborts on anything else. Mirrors the dispatch
# in get.docker.com, including its fedora-asahi-remix -> fedora alias.
get_docker_com_supports() {
    case "$1" in
        ubuntu|debian|raspbian|centos|fedora|rhel|rocky|sles|fedora-asahi-remix) return 0 ;;
        *) return 1 ;;
    esac
}

install_docker() {
    case "$PKG" in
        apt|dnf|yum)
            # Decide up front which installer applies, rather than treating every
            # get.docker.com failure as "unsupported distro": for an ID it knows,
            # let it run and surface any real failure (network, apt lock, EOL) via
            # die instead of masking it with the repo path. Only unrecognized
            # derivatives (Nobara, Mint, …) — which it would just abort on — skip
            # straight to adding Docker's repo ourselves.
            if [ -n "$OS_ID" ] && ! get_docker_com_supports "$OS_ID"; then
                info "get.docker.com doesn't support '$OS_ID' — using Docker's official repository directly."
                install_docker_ce_repo
            else
                info "Installing Docker via the official get.docker.com script"
                curl -fsSL https://get.docker.com | $SUDO sh \
                    || die "get.docker.com failed to install Docker (see the output above). Fix the issue and re-run — the script resumes."
            fi
            ;;
        pacman)
            pkg_install docker docker-compose ;;
    esac
    # Best-effort: start the daemon and let the current user run docker.
    if have systemctl; then
        $SUDO systemctl enable --now docker 2>/dev/null || true
    fi
    if [ "$(id -u)" -ne 0 ] && getent group docker >/dev/null 2>&1; then
        $SUDO usermod -aG docker "$USER" 2>/dev/null || true
    fi
}

start_docker_daemon() {
    if have systemctl; then
        $SUDO systemctl start docker 2>/dev/null || true
    elif have service; then
        $SUDO service docker start 2>/dev/null || true
    fi
}

# Resolve how to invoke docker (direct / via sudo) and make sure the daemon runs.
ensure_docker_usable() {
    if ! have docker; then
        warn "Docker is not installed."
        ask "Install Docker now?" y || die "Docker is required."
        install_docker
        have docker || die "Docker installation failed."
    fi

    docker info >/dev/null 2>&1 && { DOCKER="docker"; return; }

    # Maybe the daemon just isn't running yet.
    start_docker_daemon
    docker info >/dev/null 2>&1 && { DOCKER="docker"; return; }

    # Maybe we lack permission (not in the docker group yet, group change not
    # active in this shell) — fall back to sudo for this run.
    if [ "$(id -u)" -ne 0 ] && have sudo && sudo docker info >/dev/null 2>&1; then
        DOCKER="sudo docker"
        warn "Using 'sudo docker' for this run. To drop the sudo: 'sudo usermod -aG docker $USER' then log out and back in."
        return
    fi

    if [ "$IS_WSL" -eq 1 ]; then
        die "Docker isn't usable inside WSL. Install Docker Desktop on Windows and enable WSL integration for this distro (Settings -> Resources -> WSL integration), then re-run."
    fi
    die "Docker is installed but not usable — the daemon may be stopped or you lack permission. Try: 'sudo systemctl start docker', then re-run."
}

ensure_compose() {
    $DOCKER compose version >/dev/null 2>&1 && return
    warn "The Docker Compose v2 plugin is missing."
    case "$PKG" in
        apt|dnf|yum) ask "Install docker-compose-plugin?" y && pkg_install docker-compose-plugin || true ;;
        pacman)      ask "Install docker-compose?" y && pkg_install docker-compose || true ;;
    esac
    $DOCKER compose version >/dev/null 2>&1 \
        || die "'docker compose' is unavailable. Install Docker Compose v2 and re-run."
}

# -- node count ---------------------------------------------------------------
NODE_COUNT=10

pick_node_count() {
    if ! ( : </dev/tty ) 2>/dev/null; then
        info "Non-interactive shell — starting the recommended 10-node cluster."
        return
    fi
    cat >/dev/tty <<EOF

${BOLD}How many server nodes? (1-10)${RESET}
The full 10-node cluster is recommended — it best shows off routing across
nodes. Each node uses on the order of a few hundred MB of RAM at idle; MCP
stdio servers that a node launches can raise that. Pick fewer on a small
machine — you can always add nodes later.
EOF
    local ans
    while :; do
        printf '%sNodes [default 10]:%s ' "$BOLD" "$RESET" >/dev/tty
        read -r ans </dev/tty || ans=""
        ans="${ans:-10}"
        case "$ans" in
            [1-9]|10) NODE_COUNT="$ans"; break ;;
            *) printf 'Please enter a whole number from 1 to 10.\n' >/dev/tty ;;
        esac
    done
    info "Will start ${NODE_COUNT} server node(s)."
}

# Save choices in the auto-loaded override so plain `docker compose up -d`
# retains both the node count and optional config. Accept the old marker on
# upgrades, but never replace an override written by the operator.
OVERRIDE_MARKER="# turnstone run.sh — saved deployment choices"
LEGACY_OVERRIDE_MARKER="# turnstone run.sh — node-count limiter (safe to delete)"
CONFIG_MARKER="# Shared bootstrap config enabled."
CONFIG_ENABLED=0
managed_override() {
    local first
    first="$(head -1 "$INSTALL_DIR/compose.override.yaml" 2>/dev/null || true)"
    [ "$first" = "$OVERRIDE_MARKER" ] || [ "$first" = "$LEGACY_OVERRIDE_MARKER" ]
}

find_custom_override() {
    local f
    for f in "$INSTALL_DIR"/{compose,docker-compose}.override.{yaml,yml}; do
        [ -e "$f" ] || [ -L "$f" ] || continue
        if [ "$f" = "$INSTALL_DIR/compose.override.yaml" ] && [ ! -L "$f" ] && managed_override; then
            continue
        fi
        printf '%s' "$f"
        return 0
    done
    return 1
}

write_compose_override() {
    local f="$INSTALL_DIR/compose.override.yaml" k custom
    if custom="$(find_custom_override)"; then
        warn "$custom exists and isn't managed by this installer — leaving it as-is."
        warn "A plain 'docker compose up -d' may not match your ${NODE_COUNT}-node choice."
        return
    fi
    if [ "$NODE_COUNT" -ge 10 ] && [ "$CONFIG_ENABLED" -eq 0 ]; then
        rm -f "$f"
        return
    fi
    {
        echo "$OVERRIDE_MARKER"
        [ "$CONFIG_ENABLED" -eq 0 ] || echo "$CONFIG_MARKER"
        if [ "$NODE_COUNT" -lt 10 ]; then
            echo "# Nodes above node-${NODE_COUNT} use the 'extra' profile."
            echo "# docker compose --profile extra up -d  # start all 10"
        else
            echo "# All 10 nodes are enabled."
        fi
        echo "services:"
        if [ "$CONFIG_ENABLED" -eq 1 ]; then
            echo "  console:"
            echo "    extends: { file: compose.config.yaml, service: console }"
        fi
        for k in $(seq 1 10); do
            if [ "$CONFIG_ENABLED" -eq 1 ] || [ "$k" -gt "$NODE_COUNT" ]; then
                printf '  node-%s:\n' "$k"
                if [ "$CONFIG_ENABLED" -eq 1 ]; then
                    printf '    extends: { file: compose.config.yaml, service: node-%s }\n' "$k"
                fi
                if [ "$k" -gt "$NODE_COUNT" ]; then
                    echo '    profiles: ["extra"]'
                fi
            fi
        done
    } >"$f"
    info "Saved deployment choices in $f (a later 'docker compose up -d' honors them)."
}

# -- ports --------------------------------------------------------------------
port_in_use() {
    local p="$1"
    if have ss; then
        ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$" && return 0 || return 1
    fi
    if have lsof; then
        lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1 && return 0 || return 1
    fi
    # bash fallback: a successful connect means something is listening.
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
    return 1
}
port_free() { ! port_in_use "$1"; }

random_high_port() {
    local p
    for _ in $(seq 1 25); do
        p=$(( (RANDOM % 64000) + 1024 ))
        port_free "$p" && { echo "$p"; return; }
    done
    echo ""  # caller handles
}

pick_caddy_port() {
    # Prefer 443; rootless Docker can't bind privileged ports, so skip it there.
    if [ "$ROOTLESS" -eq 0 ] && port_free 443; then echo 443; return; fi
    port_free 8443 && { echo 8443; return; }
    local p; p="$(random_high_port)"
    [ -n "$p" ] && { echo "$p"; return; }
    echo 8443
}

pick_pg_port() {
    port_free 5432 && { echo 5432; return; }
    local p; p="$(random_high_port)"
    [ -n "$p" ] && { echo "$p"; return; }
    echo 5432
}

# -- secrets / .env -----------------------------------------------------------
gen_hex() {
    # $1 = number of bytes → 2*$1 hex chars
    if   have openssl; then openssl rand -hex "$1"
    elif have python3; then python3 -c 'import secrets,sys; print(secrets.token_hex(int(sys.argv[1])))' "$1"
    else head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

_env_get() {  # _env_get KEY DEFAULT → value from $INSTALL_DIR/.env, else DEFAULT
    local v
    v="$(grep -E "^$1=" "$INSTALL_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    printf '%s' "${v:-$2}"
}

# Resolve CADDY_PORT + PG_PORT and ensure a .env exists. On re-run the existing
# .env (secrets + ports) is reused so the rest of the script reports the ports
# the stack actually binds, not freshly-picked ones.
prepare_env() {
    if [ -f "$INSTALL_DIR/.env" ]; then
        info "Keeping existing $INSTALL_DIR/.env (secrets and ports preserved)."
        chmod 600 "$INSTALL_DIR/.env" 2>/dev/null || true
        CADDY_PORT="$(_env_get CONSOLE_HTTPS_PORT 8443)"
        PG_PORT="$(_env_get POSTGRES_PORT 5432)"
        return
    fi
    CADDY_PORT="$(pick_caddy_port)"
    PG_PORT="$(pick_pg_port)"
    local jwt pgpw
    jwt="$(gen_hex 32)"   # 64 hex chars — comfortably over the 32-char minimum
    pgpw="$(gen_hex 18)"  # hex keeps it URL-safe in the Postgres DSN
    # Write 0600 from the start (umask scoped to the subshell so it doesn't leak).
    (
        umask 077
        cat >"$INSTALL_DIR/.env" <<EOF
# Generated by run.sh — keep this private.
TURNSTONE_JWT_SECRET=$jwt
POSTGRES_USER=turnstone
POSTGRES_PASSWORD=$pgpw
# Bind published bare-metal ports (Postgres, console ACME, SearxNG) to this
# interface. 127.0.0.1 = same-host only; set your LAN IP to join from another box.
TURNSTONE_HOST_IP=127.0.0.1
POSTGRES_PORT=$PG_PORT
CONSOLE_HTTPS_PORT=$CADDY_PORT
EOF
    )
    chmod 600 "$INSTALL_DIR/.env"
    info "Wrote $INSTALL_DIR/.env (generated JWT secret + Postgres password, mode 600)."
}

# -- optional shared bootstrap config -----------------------------------------
create_shared_config() {
    local f="$INSTALL_DIR/config.toml" origin="$1" scratch
    scratch="$(mktemp -d "$INSTALL_DIR/.turnstone-bootstrap.XXXXXX")"
    # Only the inner directory is mounted. Its writable mode permits remapped
    # container root to create the file; the outer 0700 directory prevents
    # other host users from reaching it.
    mkdir -m 0777 -- "$scratch/output"
    # Generate inside the image so ownership matches its non-root user,
    # including Docker user namespace remapping.
    if ! $DOCKER run --rm --network none --user root --entrypoint python \
        -v "$scratch/output:/bootstrap:rw,z" turnstone:local \
        -m turnstone.deploy.bootstrap_config /bootstrap/config.toml "$origin" --owner turnstone; then
        rm -rf -- "$scratch"
        die "Could not create shared config. Check the HTTPS origin and Docker's write access to the install directory."
    fi
    # Rename within the same filesystem: hard links can be refused when the
    # image's UID differs from the installing user's UID.
    if ! mv -nT -- "$scratch/output/config.toml" "$f" || [ -e "$scratch/output/config.toml" ]; then
        rm -rf -- "$scratch"
        die "Could not install $f. Check directory permissions and whether the destination already exists."
    fi
    rm -rf -- "$scratch"
    info "Created $f (mode 600, owned by the container's turnstone user). Back it up privately."
    info "You may need sudo to edit or back up this file on the host."
}

prepare_shared_config() {
    local f="$INSTALL_DIR/config.toml" origin custom
    if custom="$(find_custom_override)"; then
        warn "Shared-config setup skipped: $custom is managed by you. See docs/docker.md#shared-bootstrap-config."
        return
    fi
    if managed_override && grep -qxF "$CONFIG_MARKER" "$INSTALL_DIR/compose.override.yaml"; then
        CONFIG_ENABLED=1
        # Losing the key must never silently generate a replacement on re-run.
        [ -f "$f" ] || die "Saved config is missing: $f. Restore it from backup before restarting."
        info "Keeping shared $f (including its encryption key)."
    elif ask "Prepare shared config.toml for OAuth/SSO (MCP, login, model gateways)?" n; then
        CONFIG_ENABLED=1
        if [ ! -e "$f" ] && [ ! -L "$f" ]; then
            printf 'Dashboard HTTPS origin (e.g. https://turnstone.example.com): ' >/dev/tty
            read -r origin </dev/tty || die "A dashboard HTTPS origin is required."
            create_shared_config "$origin"
        else
            info "Keeping existing $f without changing its contents or permissions."
        fi
    else
        return 0
    fi
    [ -f "$f" ] || die "Shared config must be a regular file: $f."
    # Check the actual service user's access before saving the mount or starting
    # services. Suppress parser details because the document contains secrets.
    if ! $DOCKER run --rm -i --network none --entrypoint python \
        -v "$f:/run/turnstone/config.toml:ro,z" turnstone:local - <<'PY'
import sys
import tomllib
try:
    with open("/run/turnstone/config.toml", "rb") as stream:
        tomllib.load(stream)
except (OSError, ValueError):
    sys.exit("Shared config must be valid TOML readable by the container's turnstone user.")
PY
    then
        die "Cannot load $f. Fix its permissions or TOML before re-running; see docs/docker.md."
    fi
}

# -- summary ------------------------------------------------------------------
print_done() {
    local url scale
    [ "$CADDY_PORT" = 443 ] && url="https://localhost" || url="https://localhost:${CADDY_PORT}"
    if [ "$NODE_COUNT" -lt 10 ]; then
        scale="${NODE_COUNT} of 10 nodes — pinned in compose.override.yaml, so a plain
              'docker compose up -d' keeps it. Start all 10 anytime:
              ${DIM}cd $INSTALL_DIR && $DOCKER compose --profile extra up -d${RESET}"
    else
        scale="all 10 nodes. To run fewer, stop some:
              ${DIM}cd $INSTALL_DIR && $DOCKER compose stop node-8 node-9 node-10${RESET}"
    fi
    cat <<EOF

${GREEN}${BOLD}Turnstone is running${RESET} (${NODE_COUNT} node$([ "$NODE_COUNT" = 1 ] || echo s)).

  Dashboard   ${BOLD}${url}${RESET}
              Caddy serves it with its own local CA, so your browser warns once.
              Trust it (optional):
              ${DIM}cd $INSTALL_DIR && $DOCKER compose exec caddy cat /data/caddy/pki/authorities/local/root.crt${RESET}

  Finish setup
    1. Open ${BOLD}${url}${RESET} and create the admin account when prompted —
       the first user created there gets full admin access.
    2. Log in, then add a model backend in the ${BOLD}Models${RESET} tab —
       a local server (vLLM / llama.cpp) or an OpenAI / Anthropic / Gemini key.
       Nodes boot without a model and pick it up live; no restart needed.

    ${DIM}No browser? Create the admin from the CLI instead:
    cd $INSTALL_DIR && $DOCKER compose exec node-1 turnstone-admin create-admin --username admin --name "Admin"${RESET}

  Scale       Running ${scale}

  Manage      ${DIM}cd $INSTALL_DIR${RESET}
              ${DIM}$DOCKER compose ps${RESET}        status
              ${DIM}$DOCKER compose logs -f${RESET}   logs
              ${DIM}$DOCKER compose down${RESET}      stop (add -v to wipe data)

  Config      $INSTALL_DIR/.env   (generated secrets + ports)

  Troubleshoot  ${DIM}pipx run --spec turnstone turnstone-doctor --dir $INSTALL_DIR${RESET}
                LLM-backed diagnostics for this install (read-only; needs Python)
EOF
    if [ "$CONFIG_ENABLED" -eq 1 ]; then
        info "Shared config: $INSTALL_DIR/config.toml (read-only in the console and all nodes)."
        info "Configure OAuth/SSO and delegation: https://github.com/turnstonelabs/turnstone/blob/main/docs/docker.md#shared-bootstrap-config"
    fi
}

# -- main ---------------------------------------------------------------------
main() {
    printf '%s%sTurnstone installer%s\n\n' "$BOLD" "$GREEN" "$RESET"

    detect_os
    ensure_git
    clone_repo
    ensure_docker_usable
    ensure_compose

    ROOTLESS=0
    $DOCKER info 2>/dev/null | grep -qi 'rootless' && ROOTLESS=1

    pick_node_count

    info "Building the image (first run pulls dependencies — this can take a few minutes)…"
    if ! ( cd "$INSTALL_DIR" && $DOCKER compose build ); then
        die "image build failed (see output above). Common causes: low memory or disk, or the Docker daemon stopped. Free up resources and re-run — it resumes."
    fi

    prepare_env
    info "Ports — dashboard (Caddy): ${CADDY_PORT}, PostgreSQL: 127.0.0.1:${PG_PORT}"
    prepare_shared_config
    write_compose_override

    info "Starting the stack…"
    # Plain `up -d` (honoring compose.override.yaml) + --remove-orphans so a
    # re-run that lowers the count also stops the now-excluded nodes.
    if ! ( cd "$INSTALL_DIR" && $DOCKER compose up -d --remove-orphans ); then
        die "the stack failed to start (see output above). Inspect logs: cd $INSTALL_DIR && $DOCKER compose logs"
    fi

    print_done
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
    main "$@"
fi
