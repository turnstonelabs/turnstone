# PgBouncer Connection Pooling

Turnstone cluster deployments share a single PostgreSQL instance across
all server nodes and the console. Each process
maintains a small connection pool (2 base + 3 overflow = 5 max). At
scale this adds up — a 100-node cluster opens up to 500 connections,
and a 1000-node cluster up to 5,000.

PostgreSQL's default `max_connections` is 100, and each real connection
allocates ~5–10 MB of backend memory. PgBouncer sits between turnstone
and PostgreSQL, multiplexing thousands of lightweight client connections
down to a small number of real database connections.

---

## Why PgBouncer works well with turnstone

All turnstone database operations are short-burst queries: acquire a
connection, execute 1–3 statements, commit, release. No operation holds
a connection for more than a few milliseconds. This makes **transaction
pooling mode** ideal — PgBouncer assigns a real connection only for the
duration of each transaction, then returns it to the pool.

| Cluster size | Client connections (max) | PgBouncer server connections needed |
|--------------|------------------------|-------------------------------------|
| 10 nodes     | 50                     | 10–20                               |
| 100 nodes    | 500                    | 20–40                               |
| 500 nodes    | 2,500                  | 30–60                               |
| 1,000 nodes  | 5,000                  | 40–80                               |

The server connection count stays low because most client connections
are idle at any given moment.

---

## Docker Compose

Add PgBouncer between turnstone services and PostgreSQL:

```yaml
services:
  pgbouncer:
    image: bitnami/pgbouncer:latest
    environment:
      POSTGRESQL_HOST: postgres
      POSTGRESQL_PORT: "5432"
      POSTGRESQL_DATABASE: turnstone
      POSTGRESQL_USERNAME: ${POSTGRES_USER:-turnstone}
      POSTGRESQL_PASSWORD: ${POSTGRES_PASSWORD:?}
      PGBOUNCER_POOL_MODE: transaction
      PGBOUNCER_DEFAULT_POOL_SIZE: "40"
      PGBOUNCER_MAX_CLIENT_CONN: "5000"
      PGBOUNCER_MAX_DB_CONNECTIONS: "80"
      PGBOUNCER_SERVER_IDLE_TIMEOUT: "300"
    ports:
      - "6432:6432"
    networks:
      - turnstone-net
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "pg_isready", "-h", "127.0.0.1", "-p", "6432"]
      interval: 5s
      timeout: 3s
      retries: 5
```

Then point turnstone services at PgBouncer instead of PostgreSQL
directly by changing the `DATABASE_URL` (or `TURNSTONE_DB_URL`):

```bash
# Before (direct)
TURNSTONE_DB_URL=postgresql://turnstone:secret@postgres:5432/turnstone

# After (via PgBouncer)
TURNSTONE_DB_URL=postgresql://turnstone:secret@pgbouncer:6432/turnstone
```

---

## Helm / Kubernetes

Add a PgBouncer deployment or use a Helm chart like
[bitnami/pgbouncer](https://github.com/bitnami/charts/tree/main/bitnami/pgbouncer).

In `values.yaml`, point the database at PgBouncer:

```yaml
database:
  backend: postgresql
  external:
    host: pgbouncer
    port: 6432
    database: turnstone
    username: turnstone
    existingSecret: turnstone-db-secret
```

PgBouncer configuration:

```yaml
pgbouncer:
  poolMode: transaction
  defaultPoolSize: 40
  maxClientConn: 5000
  maxDbConnections: 80
```

---

## Configuration reference

| PgBouncer setting | Recommended | Notes |
|-------------------|-------------|-------|
| `pool_mode` | `transaction` | Required — turnstone uses short-burst queries with no session state |
| `default_pool_size` | 40 | Real PostgreSQL connections per database. Start here, increase if you see `no more connections allowed` |
| `max_client_conn` | 5000 | Upper bound on client connections. Set to `cluster_nodes × 5` |
| `max_db_connections` | 80 | Hard cap on real connections to PostgreSQL. Keep below PG `max_connections` minus headroom for admin/monitoring |
| `server_idle_timeout` | 300 | Close idle server connections after 5 minutes |
| `server_lifetime` | 3600 | Recycle server connections after 1 hour |

On the PostgreSQL side:

| PostgreSQL setting | Recommended | Notes |
|--------------------|-------------|-------|
| `max_connections` | 100 | Default is fine — PgBouncer is the only client. Set higher than `max_db_connections` to leave room for admin connections |
| `shared_buffers` | 25% of RAM | Standard PostgreSQL tuning |

---

## Turnstone pool settings

Each turnstone process maintains its own SQLAlchemy connection pool to
PgBouncer (which then multiplexes to PostgreSQL):

| Environment variable | Default | Description |
|---------------------|---------|-------------|
| `TURNSTONE_DB_POOL_SIZE` | 2 | Base pool size per process |
| `TURNSTONE_DB_BACKEND` | sqlite | Set to `postgresql` for cluster deployments |
| `TURNSTONE_DB_URL` | — | Connection URL (point at PgBouncer, not PostgreSQL directly) |

The default pool of 2 + 3 overflow = 5 connections per process is
intentionally small to support large clusters. You should not need to
increase this — turnstone's database operations are all short-burst
context-managed queries that hold connections for milliseconds.

SQLAlchemy `pool_pre_ping` is enabled, so stale connections (e.g. after
PgBouncer restarts) are automatically detected and replaced.

---

## Monitoring

PgBouncer exposes stats via its admin console (connect to
PgBouncer port with user `pgbouncer`):

```sql
-- Active and waiting clients
SHOW POOLS;

-- Per-database stats
SHOW STATS;

-- Current client connections
SHOW CLIENTS;
```

Key metrics to watch:

- **`cl_active`** — clients with a server connection assigned. Should be
  well below `max_db_connections`.
- **`cl_waiting`** — clients waiting for a server connection. Sustained
  non-zero values mean you need more `default_pool_size`.
- **`sv_active`** — active server (PostgreSQL) connections. Should stay
  below PostgreSQL `max_connections`.

---

## Troubleshooting

**"no more connections allowed (max_client_conn)"** — PgBouncer is
rejecting new client connections. Increase `max_client_conn` to match
your cluster size × 5.

**"no more connections allowed (max_db_connections)"** — PgBouncer
cannot open more connections to PostgreSQL. Increase
`max_db_connections` and ensure PostgreSQL `max_connections` is higher.

**Connections timing out on startup** — If all nodes start
simultaneously, the burst of initial connections (migrations, health
checks) can temporarily exceed the pool. PgBouncer queues excess
clients by default — this resolves itself within seconds.

**Prepared statements not supported** — PgBouncer in `transaction` mode
does not support prepared statements. Turnstone's SQLAlchemy layer does
not use server-side prepared statements by default, so this is not an
issue.

See also: [Docker deployment](docker.md) · [Security](security.md)
