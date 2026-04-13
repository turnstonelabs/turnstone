# =============================================================================
# Turnstone — Docker build with uv for reproducible, locked installs
# Single image for all services: server, console, channel, eval
# =============================================================================

FROM python:3.14-slim

LABEL org.opencontainers.image.title="turnstone" \
      org.opencontainers.image.description="Multi-node AI orchestration platform"

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

# Remove the slim image's man page exclusion so man-db has actual content
RUN rm -f /etc/dpkg/dpkg.cfg.d/docker

# System dependencies: psycopg (libpq5), developer tooling for agent workflows
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    libpq5 git curl jq man-db manpages procps file \
    && rm -rf /var/lib/apt/lists/*

# Node.js LTS (for npx-based MCP servers like @modelcontextprotocol/server-github)
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Non-root user
RUN useradd --create-home --shell /bin/bash turnstone

WORKDIR /app

# Install dependencies first (cached layer — only re-runs when deps change)
COPY pyproject.toml uv.lock README.md LICENSE ./
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

# Data directory — SQLite DB is created in CWD
WORKDIR /data
RUN chown turnstone:turnstone /data

# Workspace mount point — bind-mount a host directory here
RUN mkdir -p /workspace && chown turnstone:turnstone /workspace

USER turnstone

ENTRYPOINT ["entrypoint.sh"]

# Default command (overridden per service in compose.yaml)
CMD ["turnstone-server", "--host", "0.0.0.0", "--port", "8080"]
