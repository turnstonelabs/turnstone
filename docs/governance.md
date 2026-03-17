# Governance

Turnstone governance provides role-based access control (RBAC), tool execution
policies, skills, usage tracking, and audit logging for the admin console.

## Architecture

See [diagram: 19-governance-architecture.puml](diagrams/19-governance-architecture.puml).

### RBAC (Roles & Permissions)

The permission model has two layers:

1. **Scopes** (legacy) — `read`, `write`, `approve`. Checked by `AuthMiddleware`
   on every request based on URL path classification.
2. **Permissions** (granular) — 15 permission strings checked per-endpoint by
   `require_permission()`.

**Built-in roles** (seeded by migration 008):

| Role | Permissions |
|------|-------------|
| admin | read, write, approve, admin.users, admin.roles, admin.orgs, admin.policies, admin.skills, admin.audit, admin.usage, admin.schedules, admin.watches, tools.approve, workstreams.create, workstreams.close |
| operator | read, write, workstreams.create, workstreams.close |
| viewer | read |

Custom roles can be created with any subset of the 15 valid permissions.

**Auth flow:**
1. User logs in (password or API token) → `_load_user_permissions()` aggregates
   permissions from all assigned roles
2. `_permissions_to_scopes()` derives legacy scopes (any `admin.*` → `approve`)
3. JWT created with both `scopes` and `permissions` claims
4. Middleware checks scope → handler checks permission via `require_permission()`

### Tool Policies

Admin-defined rules that control tool execution:

- **Pattern matching**: Glob syntax via `fnmatch` (e.g., `bash*`, `file_write`, `*`)
- **Actions**: `allow` (auto-approve), `deny` (block), `ask` (normal approval flow)
- **Priority**: Higher priority evaluated first, first match wins
- **Enforcement**: `evaluate_tool_policies_batch()` called in `WebUI.approve_tools()`
  before the `auto_approve` check
- **MCP granular policies**: MCP resources and prompts are evaluated using their
  `approval_label` for fine-grained control:
  - Resource reads: `mcp_resource__{uri}` (e.g., `mcp_resource__file:///docs/*` to allow,
    `mcp_resource__*` to deny all)
  - Prompt invocations: `mcp__{server}__{prompt}` (e.g., `mcp__trusted__*` to allow,
    `mcp__*` to require approval for all)
  - Built-in tools continue to use `func_name` for backward compatibility

### Skills

Admin-curated system message skills injected at workstream startup. Skills also
include session configuration (model, temperature, auto-approve, token budget,
etc.) since workstream templates were merged into the skills system in v0.8.0.

- **Runtime behavior**: Skills are loaded once at session creation and injected
  into the system message *before* user `instructions`. Skills set the baseline;
  instructions customize per-workstream behavior.
- **Default skills**: All `is_default=true` skills auto-apply to new
  workstreams, concatenated in alphabetical order by name. Use name prefixes
  (e.g. `01-safety`, `02-style`) to control ordering.
- **Explicit selection**: `--template <name>` CLI flag, `template` field on
  `POST /v1/api/workstreams/new`, console creation modal dropdown, scheduled task
  config, and channel adapter config. An explicit skill *replaces* defaults.
- **Variables**: Three built-in placeholders resolved at load time:
  `{{model}}` (active model name), `{{ws_id}}` (workstream ID),
  `{{node_id}}` (server node ID). Unrecognized placeholders are kept as-is.
- **Runtime switching**: `/template <name>` to switch, `/template clear` to revert
  to defaults, `/template` to show current. Persisted across resume.
- **Model-driven loading**: The `skill` built-in tool lets the model
  discover and activate skills mid-conversation. `search` action finds skills
  by query (auto-approved); `load` action activates by name (requires user
  approval since it changes session behavior). Main session only.
- **Categories**: general, engineering, support, custom, mcp
- **Content limit**: 32 KB per skill (enforced on create/update)
- **Storage**: `prompt_templates` table (stores skills) with JSON `variables`
  array. Migration 010 adds `template` column to `scheduled_tasks`.
- **MCP sync**: MCP server prompts auto-sync into the `prompt_templates` table
  with `origin="mcp"`, `mcp_server` set, and `readonly=True`. Manual skills take
  precedence on name collision. MCP-synced content updates reset `is_default` to
  prevent compromised servers from injecting defaults. Admin UI shows origin badge
  and disables edit/delete for MCP-sourced skills.
- **Spec fields**: Skills support the full Agent Skills standard frontmatter:
  `name`, `description`, `license`, `compatibility`, `metadata` (author, version),
  `allowed-tools`. The `license` and `compatibility` fields are preserved on import
  and editable in the admin UI. See https://agentskills.io/specification.
- **Security scanning**: Skills are automatically scanned at creation and update
  time. The scanner evaluates four risk axes: content risk (command execution,
  data exfiltration), supply chain risk (pipe-to-shell, transitive installs),
  vulnerability risk (prompt injection, insecure credentials), and declared
  capability risk (from `allowed-tools` in SKILL.md). Results populate the `scan_status`
  (safe/low/medium/high/critical) and `scan_report` (JSON breakdown) columns.
  These fields are system-managed and cannot be overwritten via the admin API.
- **Discovery**: External skills can be discovered and installed from registries:
  - `GET /v1/api/admin/skills/discover?q=...` — search the skills.sh registry
    (or a custom registry via `skills.discovery_url` setting)
  - `POST /v1/api/admin/skills/install` — install from skills.sh or GitHub.
    Fetches the `SKILL.md` file, parses YAML frontmatter, creates a skill with
    `origin="source"` and `readonly=True`, stores bundled resources.
  - Admin UI: Skills tab has "Installed" / "Discover" pill toggle.
    Discovery view has search bar, result cards, and "Import from GitHub" modal.
  - SDK: `discover_skills(q)` and `install_skill(source, skill_id=..., url=...)`
    on both Python and TypeScript console clients.

### Usage Tracking

Per-LLM-request token and tool call metrics:

- **Recording**: `on_status()` in `WebUI` records a `usage_event` after each
  LLM response with prompt/completion tokens, cache tokens, tool call count,
  model, ws_id
- **Prompt caching**: Anthropic automatic caching (`cache_control: ephemeral`)
  and OpenAI extended retention (`prompt_cache_retention: 24h` for GPT-5.x)
  are enabled by default. `cache_creation_tokens` and `cache_read_tokens` are
  tracked per request in `usage_events` and surfaced in the Usage admin tab
- **Querying**: `GET /v1/api/admin/usage` with `group_by` (day/hour/model/user)
  and time range filtering — includes cache token aggregates
- **Prometheus**: `turnstone_tokens_total{type="cache_creation|cache_read"}`
  counters on `/metrics`
- **Pruning**: `prune_usage_events(retention_days=90)` and
  `prune_audit_events(retention_days=365)` run automatically via the
  console scheduler's periodic cleanup cycle

### Audit Logging

Append-only trail of admin actions:

- **Recording**: `record_audit()` helper called from all admin mutation handlers
- **Events captured**: user.create, user.delete, token.create, token.revoke,
  channel.link, channel.unlink, role.create, role.update, role.delete,
  role.assign, role.unassign, policy.create, policy.update, policy.delete,
  template.create, template.update, template.delete,
  skill.create, skill.update, skill.delete, org.update
- **Querying**: `GET /v1/api/admin/audit` with action/user/time filters + pagination

## Database Schema

Migration 008 adds 7 tables:

| Table | Purpose |
|-------|---------|
| `orgs` | Organizations (single default org for now) |
| `roles` | Named permission bundles (3 builtin + custom) |
| `user_roles` | User-to-role assignments (composite PK) |
| `tool_policies` | Per-tool approve/deny/ask rules |
| `prompt_templates` | Reusable system message skills |
| `usage_events` | Per-request token/tool/cache metrics |
| `audit_events` | Admin action log |

Also adds `org_id` column to `users` table.

## API Endpoints

All under `/v1/api/admin/` (requires `approve` scope + granular permission).

| Group | Endpoints | Permission |
|-------|-----------|------------|
| Users / Tokens / Channels | 9 (CRUD) | `admin.users` |
| Roles | 7 (CRUD + assignment) | `admin.roles` / `admin.users` |
| Orgs | 3 (list, get, update) | `admin.orgs` |
| Tool Policies | 4 (CRUD) | `admin.policies` |
| Skills | 4 (CRUD) | `admin.skills` |
| Schedules | 6 (CRUD + runs) | `admin.schedules` |
| Watches | 3 (list, create, cancel) | `admin.watches` |
| Usage | 1 (aggregated query) | `admin.usage` |
| Audit | 1 (paginated, filtered) | `admin.audit` |

Full OpenAPI spec at `/openapi.json` and Swagger UI at `/docs`.

## Admin Console UI

6 new tabs added to the admin panel (11 total):

- **Roles** — CRUD roles, permission checkbox grid, user role assignment modal
- **Policies** — CRUD tool policies with colored action badges (green/red/amber)
- **Skills** — CRUD skills with wide modal, textarea editor
- **Usage** — Summary readouts + CSS bar chart, time range + group-by selectors
- **Audit** — Filterable log with relative timestamps, load-more pagination

Tabs are permission-gated: hidden if the user lacks the required permission.

## SDK

Both Python and TypeScript console SDKs expose governance methods:

**Python** (`TurnstoneConsole` / `AsyncTurnstoneConsole`):
- `list_roles()`, `create_role()`, `update_role()`, `delete_role()`
- `list_user_roles()`, `assign_role()`, `unassign_role()`
- `list_orgs()`, `get_org()`, `update_org()`
- `list_policies()`, `create_policy()`, `update_policy()`, `delete_policy()`
- `list_templates()`, `create_template()`, `update_template()`, `delete_template()`
- `get_usage(since, group_by=...)`, `get_audit(action=..., limit=...)`

**TypeScript** (`TurnstoneConsole`):
- Same methods with camelCase naming and typed interfaces

## Security Considerations

- **Privilege escalation prevented**: `admin_assign_role` blocks self-assignment
  and requires caller to hold a superset of the target role's permissions
- **Permission validation**: Role create/update validates permissions against
  a 15-item allowlist (`_VALID_PERMISSIONS`)
- **Self-deletion blocked**: `admin_delete_user` rejects attempts to delete
  your own account (matching the self-assignment guard on role endpoints)
- **Field allowlists**: Storage `update_*` methods filter fields against
  allowlists (`_ROLE_MUTABLE`, `_POLICY_MUTABLE`, etc.) — handler bugs
  cannot overwrite `role_id`, `builtin`, `created`, or other protected columns
- **Bootstrap safety**: `handle_auth_setup` fails and rolls back if admin role
  assignment fails, preventing locked-out first user
- **API token RBAC**: `_authenticate_api_token` loads permissions from user's
  roles, ensuring API tokens are subject to RBAC enforcement
- **Policy evaluation is fail-open**: If storage is unavailable, tool policies
  degrade to the existing approval flow (not auto-approve)
- **Audit IP resolution**: `_audit_context()` prefers `X-Forwarded-For` for
  client IP when behind a reverse proxy, falling back to `request.client.host`
