# =============================================================================
# Turnstone — Docker build with uv for reproducible, locked installs
# Single image for all services: server, console, channel, eval
# =============================================================================

FROM python:3.14-slim

LABEL org.opencontainers.image.title="turnstone" \
      org.opencontainers.image.description="Multi-node AI orchestration platform"

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /usr/local/bin/

# Remove the slim image's man page exclusion so man-db has actual content
RUN rm -f /etc/dpkg/dpkg.cfg.d/docker

# System dependencies: psycopg (libpq5), developer tooling for agent workflows.
# ripgrep is the preferred backend for the search tool — natively bounds
# per-line, per-file, and per-filesize so pathological inputs (minified
# bundles, training-data JSONL with multi-MB single records) can't OOM us.
# ffmpeg transcodes omni STT uploads (browser webm/opus) to the 16 kHz mono
# WAV the omni chat-audio lane decodes. tini is the container's PID 1 (see
# ENTRYPOINT). The version checks refuse a curl or libcurl (curl's own, and
# git's) older than trixie-security's 8.14.1-2+deb13u7, which fixes
# CVE-2026-92392, and a ca-certificates older than 20250419+deb13u1, which
# drops the root of the eMudhra CAs whose keys were compromised (DSA-6572-1);
# any newer build passes.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    libpq5 git curl jq man-db manpages procps file ripgrep ffmpeg tini \
    && for p in curl libcurl4t64 libcurl3t64-gnutls; do \
         dpkg --compare-versions "$(dpkg-query -W -f='${Version}' "$p")" ge 8.14.1-2+deb13u7 \
           || { echo "$p is older than 8.14.1-2+deb13u7" >&2; exit 1; }; \
       done \
    && { dpkg --compare-versions \
             "$(dpkg-query -W -f='${Version}' ca-certificates)" ge 20250419+deb13u1 \
           || { echo "ca-certificates is older than 20250419+deb13u1" >&2; exit 1; }; } \
    && rm -rf /var/lib/apt/lists/*

# The base image's pip checks certificates against its own certifi bundle as
# well as the system store; 26.2.2 is the first whose bundle drops emSign ECC
# Root CA - G3.
RUN uv pip install --python /usr/local/bin/python3 --system "pip>=26.2.2"

# Node.js LTS (for npx-based MCP servers like @modelcontextprotocol/server-github)
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Node checks certificates against its own built-in root list, which still has
# emSign ECC Root CA - G3 in v24.22.0, unless told to use OpenSSL's store: here
# Debian's, which the ca-certificates check above holds at or past the fix.
# stdio MCP servers inherit NODE_OPTIONS, so a value set at run time, in the
# container's environment or in an MCP server's env, must keep --use-openssl-ca.
ENV NODE_OPTIONS=--use-openssl-ca

# Non-root user
RUN useradd --create-home --shell /bin/bash turnstone

WORKDIR /app

# Install dependencies first (cached layer — only re-runs when deps change)
COPY pyproject.toml uv.lock README.md LICENSE NOTICE THIRD-PARTY-NOTICES ./
RUN uv sync --frozen --no-install-project --no-dev \
    --no-compile --extra all

# Install the project itself
COPY turnstone/ turnstone/
RUN uv sync --frozen --no-dev \
    --no-compile --extra all

# Compile bytecode in a separate step (avoids fd exhaustion during install)
RUN python -m compileall -q .venv turnstone/

# Add venv to PATH so entry points are found
ENV PATH="/app/.venv/bin:$PATH"

# Health check script (stdlib only, no pip deps needed)
COPY docker/healthcheck.py /usr/local/bin/healthcheck.py

# Entrypoint script — runs migrations before starting
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
# Data directory — SQLite DB is created in CWD
WORKDIR /data
RUN chown turnstone:turnstone /data

# Workspace mount point — bind-mount a host directory here.  The env var
# surfaces the path in the model's shell/file tool descriptions
# (config.get_workspace_dir); without it the mount is invisible to the
# model, whose cwd is /data below.
RUN mkdir -p /workspace && chown turnstone:turnstone /workspace
ENV TURNSTONE_WORKSPACE=/workspace

USER turnstone

# tini as PID 1 reaps orphaned processes, such as the helpers a stdio MCP server
# leaves behind, and forwards signals to the service.
ENTRYPOINT ["tini", "--", "entrypoint.sh"]

# Default command (overridden per service in compose.yaml)
CMD ["turnstone-server", "--host", "0.0.0.0", "--port", "8080"]
