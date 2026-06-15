# Running a bare-metal turnstone-server under systemd

These units run a `turnstone-server` **outside** Docker (e.g. on a box with a
local GPU) so it joins an existing cluster — typically the docker-compose stack
in [`compose.yaml`](../../compose.yaml). They are the hardened, production-shaped
counterpart to the quick `turnstone-server …` invocation in
[`docs/docker.md`](../../docs/docker.md) ("Join a bare-metal host").

| File | Purpose |
|------|---------|
| `turnstone-server.service` | The hardened server unit (sandboxed; secrets via `config.toml`). |
| `turnstone.slice` | Shared memory/process budget for colocated Turnstone units. |
| `turnstone-server.service.d/node.conf.example` | Per-host identity + cluster URLs drop-in (no secrets). |

## Cluster-side prerequisite

The compose stack must publish Postgres, the console's ACME endpoint, and SearxNG
on an address the bare-metal host can reach. Start it with `TURNSTONE_HOST_IP`
set to the compose host's LAN IP (default `127.0.0.1` keeps everything host-local):

```bash
TURNSTONE_HOST_IP=<compose-host-ip> docker compose up -d
```

## Install (run as root on the bare-metal host)

```bash
# 1. A dedicated, unprivileged user.
useradd --system --no-create-home --shell /usr/sbin/nologin turnstone

# 2. Install turnstone into a venv at /opt/turnstone-venv (lacme/mTLS is a core dep).
uv venv /opt/turnstone-venv --python 3.12
uv pip install --python /opt/turnstone-venv 'turnstone @ git+https://github.com/turnstonelabs/turnstone'
#   …or from a local checkout:  uv pip install --python /opt/turnstone-venv /path/to/turnstone

# 3. Secrets — match the cluster's JWT secret + DB credentials (kept out of env).
install -d -m 750 -o turnstone -g turnstone /etc/turnstone
cat > /etc/turnstone/config.toml <<'TOML'
[auth]
jwt_secret = "<same secret as the cluster>"
[database]
backend = "postgresql"
url = "postgresql+psycopg://turnstone:<password>@<compose-host-ip>:5432/turnstone"
[api]
base_url = "http://localhost:8000/v1"   # a real model backend is configured in the console UI
api_key = "dummy"
TOML
chown turnstone:turnstone /etc/turnstone/config.toml
chmod 600 /etc/turnstone/config.toml

# 4. Units + per-host drop-in.
cp turnstone-server.service turnstone.slice /etc/systemd/system/
install -d /etc/systemd/system/turnstone-server.service.d
cp turnstone-server.service.d/node.conf.example \
   /etc/systemd/system/turnstone-server.service.d/node.conf
$EDITOR /etc/systemd/system/turnstone-server.service.d/node.conf   # set the addresses

# 5. Go.
systemctl daemon-reload
systemctl enable --now turnstone-server.service
journalctl -u turnstone-server -f          # watch it register + (if the cluster runs mTLS) enroll
```

`tls.enabled` is **not** set here — a joining node inherits it from the cluster's
shared settings (the database). If the cluster runs mTLS, the node auto-enrolls a
cert from the console's ACME endpoint and re-advertises itself over `https://`.

> **mTLS + cross-host caveat:** a node on a *different* host than the console
> currently can't complete ACME enrollment — the console advertises an
> unroutable in-container address in its ACME directory
> ([turnstonelabs/lacme#22](https://github.com/turnstonelabs/lacme/issues/22)).
> Same-host bare-metal nodes, and any node in a non-mTLS cluster, are unaffected.
