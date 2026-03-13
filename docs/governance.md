# Governance

Turnstone governance provides role-based access control (RBAC), tool execution
policies, prompt templates, usage tracking, and audit logging for the admin
console.

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
| admin | read, write, approve, admin.users, admin.roles, admin.orgs, admin.policies, admin.templates, admin.audit, admin.usage, admin.schedules, admin.watches, tools.approve, workstreams.create, workstreams.close |
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

### Prompt Templates

Admin-curated system message templates injected at workstream startup:

- **Runtime behavior**: Templates are loaded once at session creation and injected
  into the system message *before* user `instructions`. Templates set the baseline;
  instructions customize per-workstream behavior.
- **Default templates**: All `is_default=true` templates auto-apply to new
  workstreams, concatenated in alphabetical order by name. Use name prefixes
  (e.g. `01-safety`, `02-style`) to control ordering.
- **Explicit selection**: `--template <name>` CLI flag, `template` field on
  `POST /v1/api/workstreams/new`, console creation modal dropdown, scheduled task
  config, and channel adapter config. An explicit template *replaces* defaults.
- **Variables**: Three built-in placeholders resolved at load time:
  `{{model}}` (active model name), `{{ws_id}}` (workstream ID),
  `{{node_id}}` (server node ID). Unrecognized placeholders are kept as-is.
- **Runtime switching**: `/template <name>` to switch, `/template clear` to revert
  to defaults, `/template` to show current. Persisted across resume.
- **Categories**: general, engineering, support, custom, mcp
- **Content limit**: 32 KB per template (enforced on create/update)
- **Storage**: `prompt_templates` table with JSON `variables` array. Migration 010
  adds `template` column to `scheduled_tasks`.
- **MCP sync**: MCP server prompts auto-sync into prompt_templates with
  `origin="mcp"`, `mcp_server` set, and `readonly=True`. Manual templates take
  precedence on name collision. MCP-synced content updates reset `is_default` to
  prevent compromised servers from injecting defaults. Admin UI shows origin badge
  and disables edit/delete for MCP-sourced templates.

### Workstream Templates

Workstream templates are behavioral profiles applied at workstream creation — the next level beyond prompt templates. While prompt templates inject system message text, workstream templates define the complete workstream configuration.

**What they define:**
- System prompt (inline text OR reference to a prompt template by name)
- Model override (empty = server default)
- Temperature, reasoning effort, max tokens, agent max turns
- Auto-approve policy (blanket and/or per-tool list)
- Token budget (0 = unlimited; warns at 80%, requires approval at 100%)
- Completion notification config (stored for v2 dispatch)

**Storage:** `workstream_templates` table (migration 011) with auto-versioning. Edits snapshot the pre-update state into `workstream_template_versions`. Workstreams record which template and version spawned them via `ws_template_id` + `ws_template_version` columns.

**Applied once at creation:** Template settings are snapshot-applied to the workstream's config. Not a live binding — template updates don't affect running workstreams.

**Prompt template drift detection:** When a workstream template references a prompt template, a SHA-256 hash of the prompt content is stored at ws_template create/update time. At workstream creation, the server compares the stored hash against current content and logs a warning on mismatch.

**Admin API:** 7 endpoints under `/v1/api/admin/ws-templates` (list, create, get, update, delete, version history) plus a read-only summary at `/v1/api/ws-templates`. Permission: `admin.ws_templates`.

**Console UI:** "WS Templates" tab (11th admin tab) with CRUD table, create/edit modals (name, description, system prompt source toggle, model, auto-approve, per-tool auto-approve, temperature, reasoning effort, max tokens, agent max turns, token budget, enabled), and version history modal. "Profile" dropdown on workstream creation modal. "WS Template" dropdown on scheduler create/edit modals.

**Token budget enforcement:** Tracked in `session.send()`. At 80% consumption, emits an info message. At 100%, the next turn requires explicit approval via the `__budget_override__` synthetic tool name (reuses existing approval UI — inline in browser, Discord buttons, bridge auto-approve). The synthetic name can be targeted by tool policies (e.g. `__budget_override__` → `allow` for admins).

**SDK:** Python (`list_ws_templates`, `create_ws_template`, `get_ws_template`, `update_ws_template`, `delete_ws_template`, `list_ws_template_versions`) and TypeScript (`listWsTemplates`, `createWsTemplate`, etc.) on both sync and async console clients. `ws_template` parameter on `create_workstream()` for both server and console SDKs.

### Usage Tracking

Per-LLM-request token and tool call metrics:

- **Recording**: `on_status()` in `WebUI` records a `usage_event` after each
  LLM response with prompt/completion tokens, tool call count, model, ws_id
- **Querying**: `GET /v1/api/admin/usage` with `group_by` (day/hour/model/user)
  and time range filtering
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
  ws_template.create, ws_template.update, ws_template.delete, org.update
- **Querying**: `GET /v1/api/admin/audit` with action/user/time filters + pagination

## Database Schema

Migration 008 adds 7 tables:

| Table | Purpose |
|-------|---------|
| `orgs` | Organizations (single default org for now) |
| `roles` | Named permission bundles (3 builtin + custom) |
| `user_roles` | User-to-role assignments (composite PK) |
| `tool_policies` | Per-tool approve/deny/ask rules |
| `prompt_templates` | Reusable system message templates |
| `usage_events` | Per-request token/tool metrics |
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
| Prompt Templates | 4 (CRUD) | `admin.templates` |
| Schedules | 6 (CRUD + runs) | `admin.schedules` |
| WS Templates | 7 (CRUD + versions + summary) | `admin.ws_templates` |
| Watches | 3 (list, create, cancel) | `admin.watches` |
| Usage | 1 (aggregated query) | `admin.usage` |
| Audit | 1 (paginated, filtered) | `admin.audit` |

Full OpenAPI spec at `/openapi.json` and Swagger UI at `/docs`.

## Admin Console UI

6 new tabs added to the admin panel (11 total):

- **Roles** — CRUD roles, permission checkbox grid, user role assignment modal
- **Policies** — CRUD tool policies with colored action badges (green/red/amber)
- **Templates** — CRUD prompt templates with wide modal, textarea editor
- **WS Templates** — CRUD workstream templates with create/edit modals, version history
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
- `list_ws_templates()`, `create_ws_template()`, `get_ws_template()`, `update_ws_template()`, `delete_ws_template()`, `list_ws_template_versions()`
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
