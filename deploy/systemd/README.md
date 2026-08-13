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
on an address the bare-metal host can reach. Use a trusted LAN or VPN interface,
firewall it to the joining node, and advertise the same reachable ACME endpoint
(default `127.0.0.1` keeps everything host-local):

```bash
TURNSTONE_HOST_IP=<compose-host-ip> \
TURNSTONE_ACME_EXTERNAL_URL=http://<compose-host-ip>:8090/acme \
  docker compose up -d
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

For a node on a different host, `TURNSTONE_ACME_EXTERNAL_URL` is required on the
console and should also be set in the node drop-in. It is the full, externally
reachable responder base
(including `/acme`) that the console embeds in the ACME protocol's follow-up
URLs and that the node trusts as an enrollment-credential destination. The
node's `TURNSTONE_CONSOLE_URL` should point at the same host and port, without
the `/acme` suffix.

For mTLS, `TURNSTONE_ADVERTISE_URL` may use a resolvable DNS hostname or a
literal IP address. Turnstone enrolls literals as IP SANs. Bracket IPv6 literals
inside URLs, for example `http://[2001:db8::10]:8080`; do not use wildcard,
unspecified, or scoped addresses as certificate identities. Restart the node
after changing its advertised identity so it enrolls a matching certificate.

The dedicated service JWT authenticates enrollment but the direct `:8090`
bootstrap is still plain HTTP/TOFU. Use HTTPS through an independently trusted
proxy when the network itself is not trusted.
