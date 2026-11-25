# Running a bare-metal turnstone-server under launchd (Apple Silicon)

The macOS counterpart to [`deploy/systemd/`](../systemd/): join a bare-metal
Apple Silicon host to an existing cluster — typically to use its unified memory
for local models. Three macOS realities shape this recipe: there is no systemd
(launchd owns services), SIP keeps `/` read-only (no `/opt`, so the venv lives
under `/Library/Turnstone`), and the system Python is 3.9 (a python.org
package provides the interpreter).

| File | Purpose |
|------|---------|
| `com.turnstone.server.plist` | The LaunchDaemon: run as `turnstone`, restart on crash, plain-file logs. Per-host addresses are `<placeholders>` to edit in place. |

## Cluster-side prerequisite

Same as [`deploy/systemd/README.md`](../systemd/README.md#cluster-side-prerequisite):
the compose stack must publish Postgres, SearxNG, and (if the cluster runs
mTLS) the console's ACME endpoint on an address the host can reach —
`TURNSTONE_HOST_IP=<compose-host-ip>` and `TURNSTONE_ACME_EXTERNAL_URL` on the
compose host, see the linked section for details.

## Install (run as an admin user via `sudo`)

### 1. Service user

```bash
PW=$(openssl rand -base64 32)
sudo sysadminctl -addUser turnstone -fullName "Turnstone" -shell /usr/bin/false \
  -home /Users/turnstone -password "$PW"
```

`sysadminctl` does not create a matching group (the primary stays gid 20
`staff`) and does not create the home directory. Fix both:

```bash
sudo dseditgroup -o create turnstone
dscl . -read /Groups/turnstone PrimaryGroupID     # note the PRINTED gid — do not assume it
sudo dscl . -create /Users/turnstone PrimaryGroupID <gid>
sudo mkdir -p /Users/turnstone
sudo chown turnstone:turnstone /Users/turnstone && sudo chmod 750 /Users/turnstone
# Optional: let the admin account update the venv later without sudo
sudo dseditgroup -o edit -a <admin-user> -t user turnstone
```

The full-disk-encryption warning `sysadminctl` may print is informational.

### 2. Python

Install a python.org 3.13 or 3.14 universal2 package (match the fleet's tested
matrix; 3.15 wheels are unverified):

```bash
curl -LO https://www.python.org/ftp/python/3.14.10/python-3.14.10-macos11.pkg
sudo installer -pkg python-3.14.10-macos11.pkg -target /    # -> /usr/local/bin/python3.14
```

Why the pkg: it installs world-readable under `/Library/Frameworks`, which a
launchd job running as `turnstone` can execute. Do not use a uv-managed Python
— those live under the invoking user's `~/.local/share` and are unreadable to
the service user — and the Command Line Tools Python is 3.9. No Xcode CLT is
needed: every dependency ships `macosx arm64` wheels (verified for the
turnstone 1.9.x dependency set — the install below compiles nothing).

### 3. uv + venv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # aarch64-apple-darwin build
UV=$HOME/.local/bin/uv
sudo $UV venv /Library/Turnstone/venv --python /usr/local/bin/python3.14
sudo $UV pip install --python /Library/Turnstone/venv turnstone   # --python selector; no activation
# If the admin user joined group turnstone in step 1, allow sudo-less upgrades:
sudo chown -R turnstone:turnstone /Library/Turnstone/venv && sudo chmod -R g+w /Library/Turnstone/venv
```

Pre-flight the commonest failure mode — the service user must be able to import the package:

```bash
sudo -u turnstone /Library/Turnstone/venv/bin/python3 -c 'import turnstone; print(turnstone.__version__)'
```

### 4. Workspace

Use a plain local directory owned by the service user (`/workspace` at the root is impossible under SIP — read-only boot volume):

```bash
sudo -u turnstone mkdir -p /Users/turnstone/turnstone-workspace
```

Do not put the workspace under `~/Desktop`, `~/Documents`, or `~/Downloads` —
macOS TCC denies daemon access to those folders even as root.

### 5. Secrets + config

Same pattern as the systemd README — `config.toml` holds the secrets, the unit
only non-secret wiring:

```bash
sudo install -d -m 750 -o turnstone -g turnstone /etc/turnstone
sudo $EDITOR /etc/turnstone/config.toml     # 0600 turnstone:turnstone
```

```toml
[auth]
jwt_secret = "<same secret as the cluster>"
[database]
backend = "postgresql"
url = "postgresql+psycopg://turnstone:<database-password>@<compose-host-ip>:5432/turnstone"   # the compose host's LAN IP, not localhost

# macOS has no /sys/class/drm or /proc, so node auto-detection cannot see the
# GPU or memory. Operator-supplied values show as source "config" in node
# listings, and without them launch/routing filters do not see this box as a
# GPU node. arch/cpu_count/os_reported are still probed natively.
[metadata]
gpu_count = 1
has_gpu = true
memory_gb = 64
cpu_model = "Apple M1 Ultra"
```

### 6. The LaunchDaemon

```bash
sudo cp com.turnstone.server.plist /Library/LaunchDaemons/
sudo $EDITOR /Library/LaunchDaemons/com.turnstone.server.plist   # replace the <placeholders>
sudo chown root:wheel /Library/LaunchDaemons/com.turnstone.server.plist
sudo chmod 0644 /Library/LaunchDaemons/com.turnstone.server.plist   # anything else: launchd silently ignores the file
sudo plutil -lint /Library/LaunchDaemons/com.turnstone.server.plist # validate before bootstrapping
sudo touch /var/log/turnstone-server.log && sudo chown turnstone /var/log/turnstone-server.log
sudo launchctl bootstrap system /Library/LaunchDaemons/com.turnstone.server.plist
```

Status: `sudo launchctl print system/com.turnstone.server`. There is no
journalctl — logs are plain files at `/var/log/turnstone-server.log`
(`tail -f`). A healthy start logs `server.service_registered` then
`node_metadata_stored`.

### 7. Post-install

```bash
sudo pmset -a sleep 0 disablesleep 1 powernap 0   # a sleeping node looks offline; assume a headless box
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate
```

If the application firewall is enabled, allow the venv's Python binary through
it (`socketfilterfw --add` + `--unblockapp`). Inference servers such as
`mlx_lm.server` default to port 8080 — plan around the server's port (e.g.
inference on 1234/8100).

## Verify

```bash
/Library/Turnstone/venv/bin/turnstone-doctor
```

The node appears in the console with `os=Darwin`. As under systemd,
`tls.enabled` is not set locally — the node inherits it from the cluster's
shared settings, and if the cluster runs mTLS the node enrolls from the ACME
endpoint. That is the only case where `TURNSTONE_CONSOLE_URL` and
`TURNSTONE_ACME_EXTERNAL_URL` in the plist are needed; leave the placeholder
addresses as-is on a plain-HTTP cluster.
