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

A node or console that can't read its settings at startup logs
`settings.load_failed` and exits instead of running on the registry defaults,
and a node that can't read its model definitions logs
`model_registry.load_failed` and exits the same way. A supervisor restart (the
shipped systemd unit's `Restart=on-failure`, a Compose restart policy,
Kubernetes) retries the read. A reload that fails later keeps the settings that
last loaded.

The console seeds a setting from its `TURNSTONE_<SECTION>_<KEY>` environment
variable (`TURNSTONE_TLS_ENABLED` for `tls.enabled`) while nothing is stored for
it; a stored value, from the Settings tab or an earlier seed, wins. An empty
variable counts as unset. Every other such variable is checked first, even one
whose setting is stored: one its setting refuses (a bool takes `true`, `1`,
`yes`, `false`, `0` or `no`) is logged as `config.seed_refused`, and the console
exits without seeding any of them. A seed that can't be written is logged as
`config.seed_failed` and ends startup too.

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

## Speech Synthesis Limits

Read-aloud (`POST /v1/api/tts`) has fixed limits rather than settings:

- **8,000 characters per request** (`_TTS_TEXT_CAP` in `turnstone/server.py`).
  Read-aloud sends a whole message in one request, so this is also the longest
  message it can read; a longer one is refused with a 400. A hosted backend may
  refuse first: OpenAI's speech models take at most 4,096 characters.
- **90 seconds for the console's wait** for a node's answer
  (`_TTS_PROXY_READ_TIMEOUT_S` in `turnstone/console/server.py`), longer than
  other forwarded requests get, because the node answers only once the whole
  clip is synthesized. At ~107 characters a second, the slowest backend
  measured, a full-length clip takes about 75 seconds.
- **80 seconds of silence from the speech backend** (`_TTS_BACKEND_TIMEOUT_S` in
  `turnstone/core/audio.py`), with no retries, so the node gives up and answers
  with its own error before the console stops waiting. A transient backend
  error fails the play; playing it again retries. A test keeps the three
  numbers in order.
- **Two clips at once per node** (`_TTS_MAX_CONCURRENT` in
  `turnstone/server.py`), each on a synthesis thread of its own, so long clips
  can't take the threads the node's other work needs. A third play is refused
  with 429 and `Retry-After`, which the browser waits out and retries twice,
  rather than queued, since queue time would come out of the console's 90
  seconds. The limit is per node and shared by every user. A play also needs one
  of the speech model's [`max_concurrency`](#per-model-concurrency) slots when
  its definition sets a limit, and is refused the same way when none is free. On
  a backend that synthesizes one clip at a time, a play started beside a long
  one waits behind it and can still time out: after 80 seconds of silence at the
  node, or at the console's 90 seconds.

A play the user replaces with another, or whose pane or tab closes, cancels its
request. The console passes that on to the node, which closes the backend call
and stops at the backend's next chunk: about two seconds after the browser
leaves, on a backend that streams. A backend that goes silent, before its
headers or between chunks, holds the play's slot until it sends again, at most
80 seconds.

### Proxies in front of the console or a node

A load balancer, ingress or reverse proxy times out on silence, not on total
duration, and the connection is silent for the whole synthesis. It has to allow
more than 90 seconds without data on this route, or a long clip ends in an HTML
504 instead of audio. The repository's AWS ECS module sets its load balancer's
`idle_timeout` to 120 seconds; some ingress controllers default to 60.

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
title generation, speech-to-text and speech synthesis—and a streaming generation
holds its slot until the stream is fully drained or closed.

Admission is strictly per alias. Two aliases remain independent even when they
point to the same URL; Turnstone does not infer shared capacity from endpoint
text. Queue time is excluded from judge/output-guard deadline accounting, and
each retry releases its slot before backoff and reacquires for the next wire
attempt. The cap is local to each process, not cluster-wide; account for the
number of nodes targeting the same inference server. Cohere/Jina reranking
selected through the Reranker role consumes the same gate as every other use of
that alias.

Voice requests wait less than generations do, because a browser is waiting on
each one. Through the console a dictation waits at most 30 seconds for a node's
answer, so dictation waits up to 15 seconds for a slot, counted from the
request's arrival (its transcode, usually tens of milliseconds, comes on top;
`_STT_ADMISSION_WAIT_S` in `turnstone/server.py`). Both dictation routes make
one backend attempt, with no SDK retries, including on `Retry-After`. Their
transcode, admission, credential minting and backend network operations share a
25-second budget from arrival (`_STT_REQUEST_TIMEOUT_S`). DNS and successive
connection attempts also consume this budget; a numeric backend address needs no
lookup. Upload, waiting for headers, and reading the response all consume that
same remainder. The transport shuts down active I/O at the deadline, even if an
upload or response is still delivering bytes. A backend error or timeout, or a
budget that runs out during the transcode or credential minting, returns 502
without a retry, so a long clip may now fail where it previously succeeded late,
even on a direct connection. A wait for a slot that runs out returns 429
instead, whichever limit ends it; the budget ends it first only after a
transcode of more than about ten seconds. Once the streamed route has begun
sending a transcript from a chat model that takes audio, its 200 is already
sent: a timeout or backend error then ends the transcript early, and one that
ends before any text reads as no speech in the browser. A play needs most of
its 90 seconds for synthesis, so read-aloud does not wait at all (see
[Speech Synthesis Limits](#speech-synthesis-limits)). When no slot comes free in
time the node answers 429 with `Retry-After`, which the browser waits out and
retries twice. A node admits at most four dictations at once: each transcodes,
waits for its slot and opens its backend call on a thread of its own
(`_STT_MAX_THREADS`), and more wait for a thread within the same 15 seconds. A
streamed transcript is then read on the node's shared workers. Transcribing an
audio attachment for a chat model that can't take audio waits as long as any
generation and keeps the model client's timeout and retries. When one alias
serves both chat and dictation, dictation waits behind chat replies; a second
alias for the same server, with its own `max_concurrency`, keeps the two apart,
at the cost of the server seeing both at once.

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
| `static` | The definition's stored `api_key`: a literal, or a `secret://` reference resolved when the registry loads (see [Secret references](security.md#secret-references)). |
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
prohibit even that fallback.

Dynamic token caches are encrypted in `oauth_tokens`, shared across nodes,
and memoized on each host. Unlinking a user's OIDC identity, or deleting the
user, purges their delegated-mode rows and memo entries; a host that could not
confirm the memo eviction is named in the reply. `entra_app` rows belong to the shared
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

### Anthropic workspace scoping

An organization-level Anthropic API key in an organization with several
workspaces is refused by the API until each request names the workspace it acts
in. Set the definition's **Workspace ID** (stored as
`capabilities.server_compat.anthropic_workspace_id`) and Turnstone sends it as
the `anthropic-workspace-id` header on every request the alias makes: model
turns, the Detect probe, and the doctor's connection check, including calls
issued with a delegated credential. Workspace-scoped keys need nothing; leave the
field empty. The value is a header, so it must be visible ASCII of at most 128
characters with no whitespace; the field only appears for the `anthropic` and
`anthropic-compatible` providers, the console refuses it elsewhere (including a
provider switch that would strand a stored value), and a value that still sits
on another provider's definition is ignored at load with a warning. Changing it
rebuilds the alias's client on the next registry reload, and context-window
auto-detection treats each workspace as its own endpoint.

### Responses output controls (per-model)

Models whose capability table declares Responses output controls expose two
additional fields in the Models create/edit shelf:

| Field | Stored capability | Values | Effect |
|-------|-------------------|--------|--------|
| Output verbosity | `verbosity` | `low`, `medium`, `high` | Controls answer length independently of reasoning effort. |
| Reasoning mode | `reasoning_mode` | `standard`, `pro` | Selects standard or higher-compute Pro execution without changing the model ID. |

An empty selection means provider default and omits the capability key. Known GPT-5.6 and GPT-6
models inherit support from the built-in table without persisting redundant support flags. An
OpenAI-compatible model pinned to the Responses API can opt in with the `supports_verbosity` and
`supports_pro_mode` capability tiles. Chat Completions and non-Responses providers do not surface or
submit these controls.

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
| `replay_reasoning_to_model` | `False` | Send stored reasoning blocks back to the provider on subsequent turns. Capability-gated: only takes effect when the model's `ModelCapabilities.supports_reasoning_replay` is also `True`. Set on canonical OpenAI gpt-5*/o-series, Grok and Anthropic Claude entries, and on the default rows of `anthropic`, for any id with no row, and of `anthropic-compatible`, so the flag takes effect there too. Models on `google` or `openai-compatible`, and unknown ones on `openai` or `xai`, default to `False`, so an operator who flips the flag on a model whose API doesn't understand reasoning replay silently no-ops rather than 400-ing. |

Edit both via the admin Models tab. See the architecture doc for the
provider-side mechanics (Anthropic `thinking`, OpenAI Responses
`reasoning` + `include=["reasoning.encrypted_content"]`, synthetic
`reasoning_text` for Chat Completions / vLLM / llama.cpp / Gemini-compat).

### Task agent overrides

`task_agent` sub-sessions resolve independently from the conversation model
so operators can pick a cheaper/faster model for autonomous loops:

| Setting | Purpose |
|---------|---------|
| `model.task_alias` | Alias used for `task_agent` sub-sessions. Falls back to `[model].task_model` in config.toml, then `[model].agent_model`, then the session's active model. When the alias isn't loaded, `task_agent` refuses the call before asking for approval instead of using another model. |
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
| `audio` | stt_model_alias, stt_prompt, tts_model_alias, tts_voice |
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
`judge.*` system settings—and use the Models tab's separate write-only flow. A
model key or MCP header may also hold a `secret://` reference, which is not
secret and is shown verbatim; see [Secret references](security.md#secret-references).

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

If a live workstream's alias is deleted, or left out of a reload, the workstream
is held on it: its next send records the message and refuses before any model
call (a reload that lands mid-send stops the send at its next model round), with
an error that names the alias and a remedy (`/model` for interactive users, an
admin for coordinators, their workstreams, scheduled runs and chat channels);
nothing runs on another model. A reopened workstream whose saved alias isn't
loaded opens held the same way, and both log one `session.model_held` warning
per registry generation. Adding the alias back causes the next send to rebind
without a process restart, and `/retry` re-sends a refused text message (one
with attachments has to be sent again). If a replacement client cannot be
constructed, the workstream is held the same way, with an error naming the
cause; Turnstone logs one `session.model_refresh_client_construction_failed`
warning per registry generation and retries only after another model reload,
avoiding a rebuild storm on every send. A fix outside the registry (a restored
certificate bundle) therefore reaches held workstreams at the next change to a
model definition, or a restart: a Sync of unchanged definitions reloads nothing.

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
