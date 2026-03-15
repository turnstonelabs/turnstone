# =============================================================================
# Turnstone — Docker build with uv for reproducible, locked installs
# Single image for all services: server, bridge, console, sim, eval
# =============================================================================

FROM python:3.14-slim

LABEL org.opencontainers.image.title="turnstone" \
      org.opencontainers.image.description="Multi-node AI orchestration platform"

COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /usr/local/bin/uv

# System dependencies for psycopg (PostgreSQL client library)
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd --create-home --shell /bin/bash turnstone

WORKDIR /app

# Compile bytecode for faster startup
ENV UV_COMPILE_BYTECODE=1

# Install dependencies first (cached layer — only re-runs when deps change)
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-install-project --no-dev \
    --extra mq --extra console --extra sim --extra postgres --extra discord --extra anthropic

# Install the project itself
COPY turnstone/ turnstone/
RUN uv sync --frozen --no-dev \
    --extra mq --extra console --extra sim --extra postgres --extra discord --extra anthropic

# Add venv to PATH so entry points are found
ENV PATH="/app/.venv/bin:$PATH"

# Health check script (stdlib only, no pip deps needed)
COPY docker/healthcheck.py /usr/local/bin/healthcheck.py

# Entrypoint script — runs migrations before starting
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Data directory — SQLite DB is created in CWD
WORKDIR /data
RUN chown turnstone:turnstone /data

USER turnstone

ENTRYPOINT ["entrypoint.sh"]

# Default command (overridden per service in compose.yaml)
CMD ["turnstone-server", "--host", "0.0.0.0", "--port", "8080"]
