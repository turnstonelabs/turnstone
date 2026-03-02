# =============================================================================
# Turnstone — multi-stage Docker build
# Single image for all services: server, bridge, console, sim, eval
# =============================================================================

# ----------------------------------------------------------------------------
# Stage 1: Builder — build the wheel
# ----------------------------------------------------------------------------
FROM python:3.13-slim AS builder

WORKDIR /build

RUN pip install --no-cache-dir hatchling

COPY pyproject.toml README.md LICENSE ./
COPY turnstone/ turnstone/

RUN pip wheel --no-deps --wheel-dir /build/wheels .

# ----------------------------------------------------------------------------
# Stage 2: Runtime — slim image with the installed package
# ----------------------------------------------------------------------------
FROM python:3.13-slim

LABEL org.opencontainers.image.title="turnstone" \
      org.opencontainers.image.description="Multi-node AI orchestration platform"

# Non-root user
RUN useradd --create-home --shell /bin/bash turnstone

# Install the wheel with all optional extras (redis for mq/console/sim)
COPY --from=builder /build/wheels/*.whl /tmp/wheels/
RUN pip install --no-cache-dir "$(ls /tmp/wheels/*.whl)[mq,console,sim]" \
    && rm -rf /tmp/wheels

# Health check script (stdlib only, no pip deps needed)
COPY docker/healthcheck.py /usr/local/bin/healthcheck.py

# Data directory — SQLite DB is created in CWD
WORKDIR /data
RUN chown turnstone:turnstone /data

USER turnstone

# Default command (overridden per service in compose.yaml)
CMD ["turnstone-server", "--host", "0.0.0.0", "--port", "8080"]
