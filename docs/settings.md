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
| **Server** (`turnstone-server`, `turnstone-bridge`) | CLI flag > ConfigStore > registry default |
| **CLI** (`turnstone`) | CLI flag > config.toml > argparse default |

The server's `apply_config()` ignores config.toml sections that overlap with
ConfigStore. A startup warning is logged for each overlapping key, directing
users to the admin Settings API.

---

## Bootstrap vs ConfigStore

**Bootstrap settings** are required before storage is available (database
connection, Redis, auth secrets, server bind address). These stay in
`config.toml` and environment variables.

| Category | Section | Where |
|----------|---------|-------|
| API credentials | `[api]` | config.toml / env |
| Database | `[database]` | config.toml / env |
| Redis | `[redis]` | config.toml / env |
| Auth | `[auth]` | config.toml / env |
| Bridge identity | `[bridge]` | config.toml / env |
| Console bind | `[console]` | config.toml / env |

**ConfigStore settings** (48 settings) are loaded from the database after
storage initialization:

| Section | Settings |
|---------|----------|
| `model` | name, temperature, max_tokens, reasoning_effort, context_window |
| `session` | instructions, retention_days, compact_max_tokens, auto_compact_pct |
| `tools` | timeout, truncation, agent_max_turns, skip_permissions, search, search_threshold, search_max_results |
| `server` | workstream_idle_timeout, max_workstreams |
| `cluster` | node_fan_out_limit, mcp_max_servers |
| `mcp` | config_path, refresh_interval, registry_url |
| `ratelimit` | enabled, requests_per_second, burst, trusted_proxies |
| `health` | backend_probe_interval, backend_probe_timeout, circuit_breaker_threshold, circuit_breaker_cooldown |
| `judge` | enabled, model, provider, base_url, api_key, confidence_threshold, max_context_ratio, timeout, read_only_tools, output_guard, redact_secrets |
| `skills` | discovery_url |
| `memory` | relevance_k, fetch_limit, max_content, nudge_cooldown, nudges |

Settings are addressed by dotted key (e.g. `memory.relevance_k`). Each has a
declared type (`int`, `float`, `str`, `bool`), optional `min_value`/`max_value`
range, optional `choices` list, and an `is_secret` flag.

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

Settings with `is_secret=True` (currently only `judge.api_key`) are blocked
from the write API with a `403` response. This prevents accidental exposure
through the admin UI or audit logs. Secret settings must be configured via
`config.toml` or environment variables.

The list endpoint masks secret values: stored secrets appear as `"***"`
rather than their actual value.

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
- Existing sessions keep their frozen configuration (settings are captured at
  workstream creation time, not read on every turn)
- Settings marked `restart_required=True` need a server restart to take effect

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
