# OpenShell Sandbox Integration

Turnstone can run inside an [OpenShell](https://github.com/NVIDIA/OpenShell)
sandbox for kernel-enforced security boundaries around tool execution. OpenShell
provides four layers of defense that Turnstone's application-level safety model
does not cover:

| Layer | Mechanism | What it prevents |
|-------|-----------|------------------|
| Filesystem | Landlock | Writes to `/etc`, `~/.ssh`, system paths |
| Network | Network namespace + seccomp + HTTP CONNECT proxy | Connections to unlisted hosts |
| Process | `setuid` drop + verification | Privilege escalation to root |
| Credentials | Proxy-level secret resolution | API keys in sandbox memory |

Turnstone's own safety layers (human approval, intent judge, tool policies,
output guard) remain active inside the sandbox and handle threats at the semantic
level -- what the LLM *means* to do with its legitimate access.

> See also: [Security and Authentication](security.md),
> [Intent Validation](judge.md), [Governance](governance.md)

---

## Quick Start

```bash
# Run turnstone-server in an OpenShell sandbox
openshell sandbox run \
  --policy deploy/openshell/turnstone-policy.yaml \
  --workdir /path/to/project \
  -- python3 -m turnstone.server --host 0.0.0.0 --port 8080
```

With inference routing (API keys never enter the sandbox):

```bash
openshell sandbox run \
  --policy deploy/openshell/turnstone-policy.yaml \
  --inference-routes deploy/openshell/routes.yaml \
  --workdir /path/to/project \
  -- python3 -m turnstone.server --host 0.0.0.0 --port 8080 \
      --base-url https://inference.local
```

The `inference.local` hostname is intercepted by the OpenShell proxy before
network policy evaluation -- no network policy entry is needed for it.

---

## Policy Files

### `deploy/openshell/turnstone-policy.yaml`

The main sandbox policy. Covers filesystem, process, and network rules.

### `deploy/openshell/routes.yaml`

Inference routing configuration. Maps `inference.local` to real LLM API
backends. Uncomment and configure the provider(s) you use.

---

## Filesystem Policy

The policy uses Landlock (Linux 5.13+) for kernel-enforced filesystem access
control. Paths are locked at sandbox creation and cannot be changed at runtime.

| Path | Access | Purpose |
|------|--------|---------|
| `--workdir` | read-write | Project files (auto-added via `include_workdir`) |
| `/tmp` | read-write | Bash tool temp scripts, eval workdirs |
| `/dev/null` | read-write | Shell redirections (`2>/dev/null`) |
| `/var/log` | read-write | Log files |
| `/usr`, `/lib`, `/lib64` | read-only | Python runtime, installed packages |
| `/etc` | read-only | System config, SSL certificates |
| `/proc`, `/dev/urandom` | read-only | Process info, entropy |
| `~/.config/turnstone` | read-only | Config file (writes go to database) |

Landlock runs in `best_effort` mode by default -- degrades gracefully on kernels
without Landlock support. Set `compatibility: hard_requirement` for production
hardened deployments.

---

## Network Policy

Default-deny. Only explicitly listed host:port pairs are reachable. All child
processes (MCP servers, bash commands, grep) inherit the network namespace and
cannot bypass the proxy.

### Included endpoints

| Policy | Hosts | Purpose |
|--------|-------|---------|
| `openai_api` | `api.openai.com` | OpenAI LLM API |
| `anthropic_api` | `api.anthropic.com` | Anthropic LLM API |
| `tavily_api` | `api.tavily.com` | Web search fallback |
| `skills_registry` | `skills.sh` | Skill discovery |
| `github_api` | `api.github.com` (read-only L7), `raw.githubusercontent.com` | Skill fetch, GitHub API |
| `mcp_registry` | `registry.modelcontextprotocol.io` (read-only L7) | MCP server discovery |
| `redis` | `127.0.0.1:6379` | Message queue |
| `web_fetch_common` | readthedocs, python docs, GitHub Pages, PyPI, npm, Stack Overflow, Wikipedia | Curated web_fetch domains |
| `bash_network_tools` | Same as `web_fetch_common` | curl/wget from bash tool |
| `package_registries` | `pypi.org`, `files.pythonhosted.org` | pip/uv package installs |
| `git_operations` | `github.com`, `gitlab.com` (L7: clone/fetch only, no push) | Git read-only operations |

### L7 enforcement

Endpoints marked with `protocol: rest` and `tls: terminate` get HTTP-level
inspection. The proxy TLS-terminates using an ephemeral per-sandbox CA, parses
each request, and evaluates method + path against the rules.

The `github_api`, `mcp_registry`, and `git_operations` policies use L7
enforcement:

- **GitHub API / MCP Registry**: `access: read-only` -- only GET, HEAD, OPTIONS
  allowed
- **Git operations**: explicit rules allowing only `info/refs` (GET) and
  `git-upload-pack` (POST) -- clone and fetch work, push is blocked

### Commented-out sections

The policy includes commented blocks for optional integrations. Uncomment and
configure as needed:

- **OIDC** -- add your identity provider's hostname
- **Discord** -- `discord.com`, `gateway.discord.gg`, `cdn.discordapp.com`
- **MCP HTTP servers** -- any MCP servers using streamable-http transport

---

## Customizing the Domain Allowlist

The `web_fetch` tool lets the LLM fetch arbitrary public URLs, but OpenShell
cannot allow "all HTTPS" -- bare wildcard hosts are rejected by policy
validation. Instead, the policy ships with a curated set of common reference
domains.

To add domains your workloads need:

```yaml
# In turnstone-policy.yaml, under web_fetch_common.endpoints:
      - host: docs.example.com
        port: 443

# Also add to bash_network_tools.endpoints if curl/wget should reach it:
      - host: docs.example.com
        port: 443
```

Wildcard patterns are supported:

- `*.example.com` -- matches one subdomain level (e.g. `api.example.com`)
- `**.example.com` -- matches any depth (e.g. `deep.sub.example.com`)

Unlisted domains return connection errors, which the LLM handles gracefully by
telling the user it cannot reach that site.

---

## Inference Routing

Inference routing keeps real API keys completely outside the sandbox. The
sandbox process only sees opaque placeholder tokens in its environment
(`openshell:resolve:env:ANTHROPIC_API_KEY`). The proxy rewrites these to real
credentials on the wire before forwarding to the upstream API.

### Setup

1. Edit `deploy/openshell/routes.yaml` -- uncomment your provider:

```yaml
routes:
  # OpenAI
  - name: inference.local
    endpoint: https://api.openai.com/v1
    model: gpt-5
    provider_type: openai
    protocols:
      - openai_chat_completions
      - model_discovery
    api_key_env: OPENAI_API_KEY

  # Or Anthropic
  - name: inference.local
    endpoint: https://api.anthropic.com
    model: claude-sonnet-4-6
    provider_type: anthropic
    protocols:
      - anthropic_messages
    api_key_env: ANTHROPIC_API_KEY
```

2. Start with `--inference-routes` and point turnstone at `inference.local`:

```bash
openshell sandbox run \
  --inference-routes deploy/openshell/routes.yaml \
  --base-url https://inference.local \
  ...
```

3. When inference routing is active, the `openai_api` and `anthropic_api`
   network policies can be removed from the sandbox policy -- the proxy handles
   LLM traffic on a separate code path that bypasses OPA entirely.

### Local model servers

For local servers (vLLM, llama.cpp) with no authentication, omit both
`api_key` and `api_key_env` from the route config. No credential resolution
is needed.

---

## MCP Server Subprocesses

MCP servers using stdio transport are spawned as child processes of turnstone.
They automatically inherit all sandbox constraints:

- **Network namespace** -- kernel-level, cannot be bypassed
- **Landlock filesystem** -- kernel-level, cannot be relaxed
- **Seccomp socket filter** -- kernel-level, inherited on fork

No per-subprocess policy entries are needed for these constraints. However, if
an MCP server makes outbound network requests (through the proxy), its binary
must appear in a `binaries[]` entry for the relevant network policy. The proxy
identifies the requesting process via `/proc/<pid>/exe` (not `argv[0]`, which
is spoofable).

Example for a Python-based MCP server that calls an external API:

```yaml
  mcp_external_api:
    name: mcp-external
    endpoints:
      - host: api.example.com
        port: 443
    binaries:
      - path: /usr/bin/python3*
      - path: /usr/local/bin/python3*
```

MCP servers using streamable-http transport are remote -- they need a network
policy entry for their host:port but no binary entry (the Python process making
the HTTP call is already covered by the standard `python3*` binary entries).

---

## Security Model: Which Layer Enforces What

```
                      OpenShell (infrastructure)          Turnstone (application)
                     ─────────────────────────────     ──────────────────────────────
Filesystem access     Landlock kernel enforcement       (no enforcement)
Network egress        Netns + seccomp + proxy + OPA     SSRF check on web_fetch
Credentials           Placeholder injection + proxy     Output guard redaction
Privilege level       setuid drop + verification        (no enforcement)
Tool semantics        (no visibility)                   Heuristic + LLM judge
Tool policies         (no visibility)                   fnmatch admin policies
Prompt injection      (no visibility)                   Output guard detection
Human approval        (no visibility)                   Approval gate + "always"
```

OpenShell constrains what the process can physically reach. Turnstone constrains
what the LLM does with its legitimate access. Neither layer is sufficient alone:

- Without OpenShell: a bash command can `curl` secrets to any endpoint, write to
  `/etc/crontab`, or read `~/.ssh/id_rsa` -- all gated only by human approval
- Without Turnstone: the LLM can `rm -rf` the entire workdir, run destructive
  commands, or consume prompt injection payloads -- all within the sandbox's
  allowed scope

---

## Hardening Checklist

For production deployments:

- [ ] Set `landlock.compatibility: hard_requirement`
- [ ] Enable inference routing (removes API keys from sandbox)
- [ ] Remove `openai_api`/`anthropic_api` network policies when using inference
      routing (traffic goes through the router, not direct)
- [ ] Review and trim `web_fetch_common` domains to your actual needs
- [ ] Remove `package_registries` policy if pip/uv installs are not needed
- [ ] Add your OIDC provider endpoint if using SSO
- [ ] Set Redis `allowed_ips` to your actual Redis host if not localhost
- [ ] Consider removing `bash_network_tools` entirely if bash should not have
      network access
