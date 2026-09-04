# System Settings

> See also: [Settings Architecture diagram](diagrams/png/24-settings-architecture.png)

The system settings feature provides database-backed configuration for server
nodes. Settings are stored in the `system_settings` table and managed through
the admin API or console Settings tab. This replaces `config.toml` for
non-bootstrap settings on server entry points, while the CLI continues to read
`config.toml` directly.

## Overview

Settings follow a typed registry pattern: every storable setting has a
`SettingDef` entry in `settings_registry.py` with type, default, description,
validation constraints, and a `restart_required` flag. Unknown keys are rejected
at the API boundary.

At runtime, `ConfigStore` loads all settings from storage into an in-memory
cache. Reads are lock-free dict lookups on an immutable snapshot. Writes acquire
a lock, persist to storage, and swap the cache atomically.

---

## Precedence

Settings resolution differs between entry points:

| Entry point | Chain |
|-------------|-------|
| **Server** (`turnstone-server`) | CLI flag > ConfigStore > registry default |
| **CLI** (`turnstone`) | CLI flag > config.toml > argparse default |

The server's `apply_config()` ignores config.toml sections that overlap with
ConfigStore. A startup warning is logged for each overlapping key, directing
users to the admin Settings API.

---

## Approval Wait Timeout

`tools.approval_timeout_seconds` controls how long a workstream waits for a
human approval decision. The default `0` disables passive timeout denial: the
workstream waits until an authorized user approves or rejects the request, or
until the workstream is cancelled or closed. Set `3600` to restore the previous
one-hour behavior.

The value is captured when each approval batch begins. A hot reload therefore
affects the next approval without changing the deadline of a request already
waiting. This setting does not make approvals durable across process restarts;
pending approval cycles remain in memory.

---

## Per-Model Sampling Overrides

The global `model.temperature`, `model.max_tokens`, and `model.reasoning_effort`
settings serve as cluster-wide defaults. Individual models can override these
via per-model settings in the `model_definitions` table (admin Models tab).

Resolution order for sampling parameters:

| Priority | Source |
|----------|--------|
| 1 (highest) | Per-model override (set in Models tab) |
| 2 | Global default (set in Settings tab) |
| 3 | Registry default (code) |

When a per-model override is `NULL` (empty in the UI), the global default is
used. Switching models via `/model <alias>` re-resolves sampling parameters
from the new model's overrides or global defaults.

### Per-model concurrency

Each model definition may set `max_concurrency` to limit simultaneous model
generations for that alias in one Turnstone process. `0` or an omitted value
means unlimited. The gate is shared by every role using the alias—interactive
turns, coordinators, task agents, judges, output guards, perception, compaction,
and title generation—and a streaming generation holds its slot until the
stream is fully drained or closed.

Admission is strictly per alias. Two aliases remain independent even when they
point to the same URL; Turnstone does not infer shared capacity from endpoint
text. Queue time is excluded from judge/output-guard deadline accounting, and
each retry releases its slot before backoff and reacquires for the next wire
attempt. The cap is local to each process, not cluster-wide; account for the
number of nodes targeting the same inference server. Cohere/Jina reranking
selected through the Reranker role consumes the same gate as every other use of
that alias. Direct STT/TTS protocol calls do not consume this generation cap.

### Judge batch parallelism

`judge.parallel_evaluations` controls how many independent tool calls from one
approval batch the intent judge evaluates concurrently. It is an integer from
1 through 16 and defaults to 1, preserving serial evaluation until an operator
opts into wider fan-out. Changes are hot-read at the next batch; work already
in flight keeps its captured worker count.

This is a per-batch fan-out setting, not another backend capacity limit. The
judge model alias's `max_concurrency` gate still caps total generations across
all judge batches and every other role using that alias. Actual overlap is
therefore bounded by the batch size, `judge.parallel_evaluations`, and available
alias admission slots. A smaller positive alias cap also narrows the batch's
worker pool so excess judge threads do not queue ahead of later alias traffic.

### Model backend authentication

Model definitions support four backend credential modes:

| `auth_mode` | Identity sent to the model gateway |
|-------------|------------------------------------|
| `static` | The definition's stored `api_key`. |
| `entra_obo` | A caller-delegated Entra access token minted from that user's captured OIDC credential. |
| `entra_app` | A shared app-identity token minted with Turnstone's OIDC client credentials. |
| `rfc8693_obo` | A caller-delegated access token minted from the captured credential via RFC 8693 token exchange, requesting the definition's `obo_scopes`. |

Dynamic modes require an exact `obo_audience` resource identifier. Before an
admin can save one, an operator must add that literal audience to
`model.auth_audience_allowlist` (comma- or newline-separated). Wildcards and
base-URL host matching are intentionally unsupported, and a row whose
effective mode is `static` refuses to store a new non-empty `obo_audience` on
either create or update — an audience cannot be staged for a later flip
(clearing a stale value, or re-saving it unchanged, stays allowed).
`obo_scopes` follows the same staging rule with the mode set inverted: only
`rfc8693_obo` reads it, so every other effective mode refuses to store a new
non-empty value, while clearing or re-saving one unchanged stays open. The
value itself is optional and shape-checked only — whether it satisfies the
IdP is decided at mint time. On a row that is (or becomes) dynamic, every
change except the tuning fields — context window, temperature, max tokens,
reasoning effort, and the two reasoning-persistence toggles — also requires
`admin.mcp`; service tokens do not bypass this capability-escalation gate.
The one exception is de-escalation: a save whose only gated change is
switching `enabled` off is a pure disable, needs only `admin.models`, and
skips validation — a de-listed audience must never block disarming its own
row. The gate is deny-by-default: a field counts as auth-relevant unless it
is provably neutral, so re-enabling a disabled dynamic row, re-pointing its
`base_url`, or swapping its provider or alias all escalate.

Validation runs in two tiers, matching the MCP `oauth_obo` write rules. Row
validity — the audience is allow-listed — applies to every gated write that
touches a dynamic configuration, so a revoked audience can be neither silently
re-pointed at a new `base_url` nor re-armed by an enable flip. Deployment
posture — the token encryption key installed, single sign-on configured, and
the grant profile valid and able to carry the mode — is checked when a write
*chooses* the mode/audience pair and when it re-enables a disabled dynamic
row (arming is the flip that resumes minting, so it must meet what minting
needs); other edits to an existing row stay open if the deployment's posture
changed after it was saved (its mints warn at runtime instead). Refusals name
their cause and echo the configured value.

One asymmetry to be aware of: the write path counts a transient discovery
outage (`enabled=false`, retryable) as configured, but the mints themselves
require discovery to have completed — a config saved during an outage starts
minting only once any authenticated request heals discovery. Until then calls
warn and follow the fail-open/fail-closed policy above.

Every dynamic mode pairs with exactly one grant profile: `entra_obo` and
`entra_app` require `[oidc] obo_grant_profile = "entra"`, and `rfc8693_obo`
requires `"rfc8693"`. The pairing is enforced at the posture tier, so a row
saved before the rule existed keeps accepting same-pair edits; its mints
refuse at runtime with `cause=grant_profile_mismatch` and no IdP traffic.
Judge, output-guard, perception, utility, and sub-agent lanes inherit the
session's effective user for the delegated modes. The perception memo is
partitioned by that principal as well as alias and content hash, so a result
authorized as one user cannot be served to another. Scheduled and wake-driven
work retains the workstream owner even when no user is connected. Eval and
optimizer lanes are registry-less development tools and therefore do not use
dynamic model authentication.

`entra_app` is an explicit model-definition choice; Turnstone never changes a
failed or ownerless delegated call into a client-credentials grant. A
delegated-mode call with no effective user always refuses. A dynamic alias
without a real static key also always refuses instead of issuing its
SDK-construction placeholder. When a real static key is explicitly configured,
mint failures may use it by default; set `model.auth_fail_closed = true` to
prohibit even that fallback. A refusal is not routed through the model
fallback chain.

Dynamic token caches are encrypted in `mcp_user_tokens`, shared across nodes,
and memoized on each host. Unlinking a user's OIDC identity purges their
delegated-mode rows and memo entries. `entra_app` rows belong to the shared
`__app__` identity and are not user-deprovisioned; after client-credential
revocation, an already-minted app bearer remains usable until its recorded
expiry.

Each model call resolves its dynamic credential against the immutable model
definition snapshot that supplied that call's provider, client, endpoint, and
model ID. An admin edit can therefore never pair an old `base_url` with a new
audience, grant mode, or static-key fallback input. The principal and token
remain per-call/live; the connection and model-owned auth configuration move
together as one binding on the next operation. The deployment-wide
`model.auth_fail_closed` switch is intentionally read live on every mint, so an
operator can tighten fallback policy immediately without rebuilding sessions.

`obo_audience` and `obo_scopes` are literal and capped at 2048 characters
each. Environment-variable expansion is deliberately not applied, so the
allow-list decision cannot vary by node or expand beyond the persisted
boundary.

### Responses output controls (per-model)

Models whose capability table declares Responses output controls expose two
additional fields in the Models create/edit shelf:

| Field | Stored capability | Values | Effect |
|-------|-------------------|--------|--------|
| Output verbosity | `verbosity` | `low`, `medium`, `high` | Controls answer length independently of reasoning effort. |
| Reasoning mode | `reasoning_mode` | `standard`, `pro` | Selects standard or higher-compute Pro execution without changing the model ID. |

An empty selection means provider default and omits the capability key. Known
GPT-5.6 models inherit support from the built-in table without persisting
redundant support flags. An OpenAI-compatible model pinned to the Responses API
can opt in with the `supports_verbosity` and `supports_pro_mode` capability
tiles. Chat Completions and non-Responses providers do not surface or submit
these controls.

**Removed settings:** `model.name` and `model.context_window` have been removed
from ConfigStore. Model names and context windows are now configured per-model
in the Models tab. A startup warning is logged if these keys appear in
`config.toml`.

### Reasoning persistence (per-model)

Two boolean flags on `model_definitions` (migration 052) control how
reasoning text round-trips per model:

| Flag | Default | Effect |
|------|---------|--------|
| `surface_persisted_reasoning` | `True` | Surface stored reasoning text on `/history` payloads so a page reload re-renders the reasoning bubble. **Storage of reasoning bytes is independent of this flag** — they ride in `provider_data` regardless. |
| `replay_reasoning_to_model` | `False` | Send stored reasoning blocks back to the provider on subsequent turns. Capability-gated: only takes effect when the model's `ModelCapabilities.supports_reasoning_replay` is also `True`. Set on canonical OpenAI gpt-5*/o-series and Anthropic Claude entries; unknown / local-server models default to `False` so an operator who flips the flag on a model whose API doesn't understand reasoning replay silently no-ops rather than 400-ing. |

Edit both via the admin Models tab. See the architecture doc for the
provider-side mechanics (Anthropic `thinking`, OpenAI Responses
`reasoning` + `include=["reasoning.encrypted_content"]`, synthetic
`reasoning_text` for Chat Completions / vLLM / llama.cpp / Gemini-compat).

### Task agent overrides

`task_agent` sub-sessions resolve independently from the conversation model
so operators can pick a cheaper/faster model for autonomous loops:

| Setting | Purpose |
|---------|---------|
| `model.task_alias` | Alias used for `task_agent` sub-sessions. Falls back to `[model].agent_model` in config.toml, then the session's active model. |
| `model.task_effort` | Reasoning effort for `task_agent`. Empty string means "inherit from the session". |

Both are live-editable from the Settings tab and take effect on the
next sub-agent invocation — no restart required.

---

## Bootstrap vs ConfigStore

**Bootstrap settings** are required before storage is available (database
connection, Redis, auth secrets, server bind address). These stay in
`config.toml` and environment variables.

| Category | Section | Where |
|----------|---------|-------|
| API credentials (CLI only) | `[api]` | config.toml / env — the server takes endpoints from model definitions |
| Database | `[database]` | config.toml / env |
| Auth | `[auth]` | config.toml / env |
| Console bind | `[console]` | config.toml / env |

**ConfigStore settings** are loaded from the database after storage
initialization:

| Section | Settings |
|---------|----------|
| `model` | default_alias, auth_audience_allowlist, auth_fail_closed, temperature, max_tokens, reasoning_effort, task_alias, task_effort |
| `session` | instructions, retention_days, compact_max_tokens, auto_compact_pct |
| `tools` | timeout, approval_timeout_seconds, truncation, agent_max_turns, skip_permissions, search, search_threshold, search_max_results |
| `server` | workstream_idle_timeout, max_workstreams |
| `cluster` | node_fan_out_limit, mcp_max_servers |
| `mcp` | config_path, registry_url, oauth_allow_private_network |
| `ratelimit` | enabled, requests_per_second, burst, trusted_proxies |
| `health` | backend_probe_interval, backend_probe_timeout, circuit_breaker_threshold, circuit_breaker_cooldown |
| `judge` | enabled, model, smart_approvals, confidence_threshold, max_context_ratio, timeout, parallel_evaluations, read_only_tools, output_guard, output_guard_budget_seconds, output_guard_llm, output_guard_model, output_guard_llm_timeout, redact_secrets, cancel_on_approval |
| `interface` | close_tab_action, theme |
| `skills` | discovery_url |
| `memory` | relevance_k, index_budget_chars, model_index_over_budget_notice, max_content, nudge_cooldown, nudges |

Settings are addressed by dotted key (e.g. `memory.relevance_k`). Each has a
declared type (`int`, `float`, `str`, `bool`), optional `min_value`/`max_value`
range, optional `choices` list, and an `is_secret` flag.

A stored value outside a setting's range is clamped to the nearest bound when
settings load, with a warning, so a bound tightened by an upgrade never
silently replaces an operator's choice with the default. Values of the wrong
type or outside a `choices` list are skipped with a warning and the default
applies.

---

## Storage

The `system_settings` table (migration 015) stores settings as JSON-encoded
values with a composite primary key of `(key, node_id)`:

| Column | Type | Description |
|--------|------|-------------|
| `key` | text | Dotted setting key (e.g. `model.temperature`) |
| `value` | text | JSON-encoded value |
| `node_id` | text | Node ID for per-node overrides (empty string = global) |
| `is_secret` | int | 1 if the setting contains secrets |
| `changed_by` | text | Username of last editor |
| `created` | text | ISO timestamp |
| `updated` | text | ISO timestamp |

Per-node overrides layer on top of global settings. When `ConfigStore` loads,
it fetches global settings first, then overlays per-node values.

---

## Admin API

Four endpoints on the **console** server, all requiring the `admin.settings`
permission.

### `GET /v1/api/admin/settings`

List all settings with their effective values, defaults, and metadata.

**Response:** `200`

```json
{
  "settings": [
    {
      "key": "model.temperature",
      "value": 0.7,
      "source": "storage",
      "type": "float",
      "description": "Sampling temperature",
      "section": "model",
      "is_secret": false,
      "node_id": "",
      "changed_by": "admin",
      "updated": "2026-03-14T10:00:00",
      "restart_required": false
    }
  ]
}
```

---

### `GET /v1/api/admin/settings/schema`

Return the full registry catalog (all defined settings with metadata). Useful
for building dynamic admin UIs.

**Response:** `200`

```json
{
  "schema": [
    {
      "key": "model.temperature",
      "type": "float",
      "default": 0.5,
      "description": "Sampling temperature",
      "section": "model",
      "is_secret": false,
      "min_value": 0.0,
      "max_value": 2.0,
      "choices": null,
      "restart_required": false
    }
  ]
}
```

---

### `PUT /v1/api/admin/settings/{key}`

Update a setting. The value is validated against the registry (type coercion,
range, choices). Secret settings (`is_secret=true`) cannot be written via the
API -- they must be configured via config.toml or environment variables.

**Path parameters:**

| Parameter | Type   | Description |
|-----------|--------|-------------|
| `key`     | string | Dotted setting key (e.g. `model.temperature`) |

**Request body:**

```json
{
  "value": 0.7,
  "node_id": ""
}
```

| Field     | Type   | Required | Default | Description |
|-----------|--------|----------|---------|-------------|
| `value`   | any    | yes      | --      | New value (type-coerced against registry) |
| `node_id` | string | no       | `""`    | Node ID for per-node override |

**Response (success):** `200`

```json
{
  "key": "model.temperature",
  "value": 0.7,
  "source": "storage",
  "type": "float",
  "description": "Sampling temperature",
  "section": "model",
  "is_secret": false,
  "node_id": "",
  "changed_by": "admin",
  "updated": "",
  "restart_required": false
}
```

**Errors:**

| Status | Condition |
|--------|-----------|
| 400    | Unknown key, invalid value, type mismatch, out of range |
| 403    | Secret setting (must use config.toml or env) |

---

### `DELETE /v1/api/admin/settings/{key}`

Reset a setting to its registry default by removing it from storage.

**Path parameters:**

| Parameter | Type   | Description |
|-----------|--------|-------------|
| `key`     | string | Dotted setting key |

**Query parameters:**

| Parameter | Type   | Required | Default | Description |
|-----------|--------|----------|---------|-------------|
| `node_id` | string | no       | `""`    | Node ID (empty = global) |

**Response (success):** `200`

```json
{"status": "ok", "key": "model.temperature", "default": 0.5}
```

**Response (not found):** `404`

```json
{"error": "Setting 'model.temperature' has no stored value"}
```

---

## Secret Settings

The registry currently defines no production secret system setting. The generic
machinery nevertheless treats any future `is_secret=True` entry as write-only:
list and write responses return `"***"`, and submitting that sentinel preserves
the stored value. Model API keys are fields on model definitions—not
`judge.*` system settings—and use the Models tab's separate write-only flow.

---

## Hot Reload

`ConfigStore` caches all settings in memory for fast, lock-free reads. To
refresh the cache after external changes (e.g. direct database edits or
cluster-wide propagation):

```
POST /v1/api/_internal/config-reload
```

This triggers `ConfigStore.reload()`, which re-reads all settings from storage
and atomically swaps the cache. The `version` counter increments on every
reload.

**Behavior after reload:**

- New workstreams pick up updated values immediately (via `session_factory`)
- Most workstream/session settings remain the snapshot captured at creation or
  resume. Component docs call out deliberate live-read exceptions; for
  example, Smart Approval settings and the human approval timeout are
  snapshotted at the start of each approval batch.
- Settings marked `restart_required=True` need a server restart to take effect

### Model-definition reloads

The Models tab has a separate live-reload contract from ordinary ConfigStore
settings. Existing sessions remember the concrete registry generation that
supplied their active alias and re-resolve that alias at the start of the next
send. Endpoint, provider, backend model ID, capabilities, extra parameters, and
backend-auth configuration are replaced as one immutable binding. In-flight
turns, judges, and task agents finish or cancel against the binding they
started with; an admin edit never tears one request across two definitions.
The alias's admission gate is retained and resized in place, so a concurrency
edit preserves in-flight accounting and does not reset cached judges or the
output-guard rate limiter.

The Reranker role is resolved from one coherent ConfigStore snapshot for each
retrieval batch, so existing sessions observe alias or instruction changes
without reconstruction. A relevant model-definition edit retires the old
reranker transport and lets active requests drain on their immutable lanes;
cap-only and unrelated edits keep the pooled transport warm.

Sampling and other saved workstream configuration remain workstream state. A
model-definition edit does not silently rewrite a live workstream's chosen
temperature, reasoning effort, max tokens, skill, or persona. Use
`/model <alias>` (or create/fork a workstream) when an explicit session-level
model switch is intended.

If a live workstream's alias is deleted, its next send first attempts the
configured fallback chain. Without a usable fallback, the operator-facing
error names the removed alias and points interactive users to `/model`; adding
the alias back causes the next send to rebind without a process restart. If a
replacement client cannot be constructed, Turnstone logs one
`session.model_refresh_client_construction_failed` warning per registry
generation and retries only after another model reload, avoiding a rebuild
storm on every send.

---

## Migration from config.toml

On startup, `warn_migrated_settings()` scans `config.toml` for keys that are
now managed by ConfigStore. Each overlap produces a warning:

```
WARNING config.toml [model] temperature is now managed via Settings API —
this value will be ignored. Use the admin Settings tab or
PUT /v1/api/admin/settings/model.temperature to configure.
```

To migrate:

1. Note the values from `config.toml` for sections that overlap with ConfigStore
2. Use `PUT /v1/api/admin/settings/{key}` or the console Settings tab to set
   each value
3. Remove the migrated sections from `config.toml`
4. Restart the server to verify no warnings

---

## SDK

### Python

```python
from turnstone.sdk import TurnstoneConsole

with TurnstoneConsole("http://localhost:9090", token="tok_xxx") as admin:
    # List all settings with effective values
    result = admin.list_settings()
    for s in result["settings"]:
        print(f"{s['key']} = {s['value']} (source: {s['source']})")

    # Get the schema catalog
    schema = admin.get_settings_schema()

    # Update a setting
    admin.update_setting("model.temperature", value=0.7)

    # Update with per-node override
    admin.update_setting("model.temperature", value=0.3, node_id="node-2")

    # Reset to default
    admin.delete_setting("model.temperature")
```

### TypeScript

```typescript
import { TurnstoneConsole } from "@turnstone/sdk";

const admin = new TurnstoneConsole({
  baseUrl: "http://localhost:9090",
  token: "tok_xxx",
});

// List all settings
const result = await admin.listSettings();
for (const s of result.settings) {
  console.log(`${s.key} = ${s.value} (source: ${s.source})`);
}

// Get schema catalog
const schema = await admin.getSettingsSchema();

// Update a setting
await admin.updateSetting("model.temperature", { value: 0.7 });

// Reset to default
await admin.deleteSetting("model.temperature");
```

---

## Architecture

See [Settings Architecture diagram](diagrams/png/24-settings-architecture.png)
for the full data flow covering server startup, admin API writes, hot reload,
and settings precedence.
