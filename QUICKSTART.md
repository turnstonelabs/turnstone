# Quickstart

Install Turnstone, then diagnose it with `turnstone-doctor` if anything looks off.

## Install

The one-line installer autodetects your distro (Ubuntu/Debian, Fedora/RHEL,
Arch, and WSL), installs git + Docker if missing, generates secrets, picks free
ports, and starts the stack:

```bash
curl -fsSL https://raw.githubusercontent.com/turnstonelabs/turnstone/main/run.sh | bash
```

Re-running is safe — it updates the checkout and keeps your existing `.env`.
When it finishes it prints the dashboard URL and how to create the first admin
user.

**Other ways to install**

- **Already have Docker?** Clone the repo and `docker compose up` for the full
  local cluster, or `docker compose -f turnstone/deploy/compose.yaml up` for the
  released single-node stack. See [docs/docker.md](docs/docker.md).
- **Python package:** `pip install turnstone` (add `--pre` for the experimental
  track), then run `turnstone-server` / `turnstone-console` directly. See the
  [README](README.md#quickstart).

## Diagnose: `turnstone-doctor`

`turnstone-doctor` is an LLM-backed assistant that inspects a **running**
Turnstone install and helps you troubleshoot it. It is **read-only** — it
investigates and tells you the exact commands to fix things, but never changes
your system. (Installation is the installer's job, not the doctor's.)

```bash
# From a host that has the turnstone package installed:
turnstone-doctor

# For a Docker install from run.sh (no package on the host), run it with pipx:
pipx run --spec turnstone turnstone-doctor --dir ~/turnstone
```

### What it does

1. **Preflight** — detects how Turnstone is installed here (docker-compose,
   systemd/bare-metal, pip, or a source checkout) by probing for `config.toml`
   files, `TURNSTONE_*` environment variables, compose files, and systemd units.
2. **Self-configures its LLM** — it powers its own brain from your cluster's
   *own* model configuration (env / `config.toml` / the database). Whether that
   works is the first diagnostic: success means your LLM backend is healthy; if
   it can't, that's surfaced as finding #1 and it falls back to asking you for a
   provider and key so it can still help.
3. **Version check** — reports the installed version, version drift across your
   cluster's nodes, and the latest upstream stable/experimental releases.
4. **Interactive diagnosis** — it reads logs, `/health`, `docker compose ps`,
   `systemctl`, config, and ports to pin down problems like a node not joining
   the console, an unreachable database, a down model backend, port conflicts,
   or a JWT-secret mismatch — then hands you the precise remediation commands.

### Flags

| Flag | Purpose |
|------|---------|
| `--dir PATH` | Install directory to inspect (default: current directory) |
| `--report` | Print the deterministic preflight report and exit — no LLM key needed |
| `--offline` | Skip the upstream GitHub version check |

`--report` is the fastest way to get a health snapshot (and to share one when
asking for help) — it never needs an API key:

```bash
turnstone-doctor --report --dir ~/turnstone
```

```
## Install profile
- Detected kind(s): docker-compose  (primary: docker-compose)
- Docker daemon reachable: yes
- Compose files:
    /home/you/turnstone/compose.yaml
- Database: backend=postgresql, url=postgresql+psycopg://turnstone:****@postgres:5432/turnstone
- Candidate health URLs: http://localhost:8080/health, http://localhost:8090/health

## Versions
- Installed (this tool): 1.7.0a2
- Cluster nodes: 10 reporting; versions ['1.7.0a2']
- Version drift across nodes: no
- Upstream: stable 1.6.9, experimental 1.7.0a2

## LLM backend (ok)
- resolved Qwen/Qwen3-32B via openai-compatible @ http://host.docker.internal:8000/v1
```

Secrets (JWT secret, database password, API keys) are always redacted in the
report and in anything the doctor reads.

## Tips

- **Type `quit`** to exit the conversation; **Ctrl+C** interrupts (twice to quit).
- **Point it at the right install** with `--dir` when you run it from elsewhere.
- **(Re)installing or adding nodes?** Use the installer (`run.sh`), not the doctor.

## See Also

- [Docker Deployment](docs/docker.md) — compose stacks, ports, and bare-metal nodes
- [Security](docs/security.md) — auth architecture and token types
- [Governance](docs/governance.md) — roles, policies, and templates
