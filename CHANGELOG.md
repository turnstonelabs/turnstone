# Changelog

All notable changes to turnstone are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) for
version numbers (`X.Y.Z`, with `X.Y.ZaN` / `bN` / `rcN` for pre-releases).

Three release tracks are maintained:

- **`stable/1.4`** — patch-only (`v1.4.x`)
- **`stable/1.5`** — patch-only (`v1.5.x`)
- **`main`** — experimental (next major)

## [Unreleased]

## [1.5.15]

### Fixed

- **Admin console blank-page on MCP server rows with consented users** — a
  Phase 9 (1.5.14) regression in `admin.js` used double-quote string
  delimiters on the bulk-revoke button HTML literal, but the literal embeds
  a `"` mid-attribute. JS closed the string early, turned `bulk-revoke (`
  into bare tokens, and the resulting `SyntaxError` wiped out every global
  in `admin.js` — `showAdmin` and all other admin entry points became
  undefined, so the console UI was non-functional whenever the rendered MCP
  server list contained at least one row with `consented_users_count > 0`.
  Switch the literal to single-quote delimiters to match the surrounding
  block.

## [1.5.14]

Backports OAuth-MCP Phase 9 from `main` to the `stable/1.5` track.

### Added

- **OAuth-MCP Phase 9 — admin status, deferred-consent persistence, operator
  docs** — completes the per-(user, server) OAuth-MCP build-out. The sync pool
  dispatchers now upsert into a new `mcp_pending_consent` table on
  `mcp_consent_required` / `mcp_insufficient_scope`, so a non-interactive run
  (scheduled / channel) that hits an unconsented server surfaces the deferred
  prompt to the user on their next dashboard load via the gear-icon badge —
  rows are cleared automatically by the OAuth callback handler on consent
  completion, or via new DELETE endpoints for manual dismiss. The MCP Servers
  admin row gains a `consented_users_count` pill and a two-step-confirm
  bulk-revoke button for `auth_type=oauth_user` servers (upstream RFC 7009
  revoke is intentionally not attempted in bulk to avoid N synchronous
  round-trips against the provider). Operator-facing docs land at
  `docs/mcp-oauth.md` and `docs/operations/mcp-oauth-headless.md`.

  Introduces forward-only migrations `054_mcp_pending_consent` and
  `055_mcp_user_tokens_server_index`.

## [1.5.13]

This release introduces one forward-only schema migration:
`053_services_notify_trigger` — installs the `services_notify` PostgreSQL
trigger that backs the new LISTEN/NOTIFY dispatcher (no-op on SQLite, where
the dispatcher uses in-process fan-out).

### Added

- **Reactive node discovery via PG LISTEN/NOTIFY** — the console gains a
  `NotifyDispatcher` that holds a dedicated session-mode PostgreSQL `LISTEN`
  connection (bypasses pgbouncer transaction pooling) and fans wake-ups out to
  per-channel handlers on a separate dispatch thread. The cluster collector
  subscribes to a new `services` channel and reacts to node register /
  deregister within ~500 ms instead of waiting up to 60 s for the next discovery
  loop; the 60 s loop is retained as the backstop for crash-shaped loss
  (NOTIFY only fires on real writes). The storage layer also gains a uniform
  `notify` / `listen` API with an SQLite synthetic-sweep fallback so consumer
  code is identical across backends. `TURNSTONE_DB_LISTEN_URL` (or
  `[database] listen_url` in `config.toml`) points the dispatcher at a
  direct-to-Postgres URL; defaults to the main DB URL when unset.
- **Event-driven `wait_for_workstream`** — coord's block-wait tool no longer
  polls storage every 500 ms. A new in-process `ChildEventBus` notifies waiters
  whenever a child state change is dispatched to the UI, and the wait loop
  blocks on `threading.Event.wait` with a 2 s heartbeat cap (matching the
  existing `wait_progress` SSE cadence). A 600 s wait that previously hit
  storage ~2400 times now wakes only on real state transitions, with ~4× lower
  SSE traffic in the quiescent case.
- **Memory tool audit trail** — the memory tool now emits `memory.save`,
  `memory.update`, and `memory.delete` audit events (the admin-console DELETE
  route previously emitted only `memory.delete`, so tool-initiated mutations
  had no audit footprint). All emissions are best-effort and never break the
  tool call itself.
- **`task_agent` per-call personas via `skill=`** — `task_agent` now accepts
  an optional `skill=<name>` argument that loads the named skill's content as
  the sub-agent's persona in place of the hardcoded identity statement. The
  fixed operating-guidance block (one-shot, tool-use over narration,
  no follow-up questions) is still layered on top of every persona. High- and
  critical-risk skills surface their risk tier in the approval header and
  emit a `task_agent.high_risk_skill` warning, matching the existing
  session-load gate.

### Fixed

- **Per-role plan / task model overrides could be bypassed by the LLM** — the
  back-compat `default` alias auto-synthesised by `load_model_registry`
  remained visible to the model even when an operator had configured
  `model.task_alias` / `model.plan_alias`, so `task_agent(model="default")`
  routed to whichever backend the synthesised alias was attached to at boot
  instead of the configured per-role default. The synthesised alias is now
  only added when neither the DB nor `[models.*]` populates the registry,
  filtered out of the LLM-visible alias list, and explicitly rejected at the
  validator chokepoint as defense-in-depth.
- **Mermaid streaming parse errors + progressive `hljs`** — live-streamed
  mermaid blocks with bare `(`, `[`, `{` inside unquoted edge or rectangle
  node labels were re-entering the shape parser and producing
  `Parse error, got 'PS'` messages. The renderer now autoquotes the two
  affected label forms (`|content|` and `ID[content]`) before the SVG cache
  lookup; shapes whose syntax already nests delimiters (cylinders, subroutines,
  trapezoids, etc.) are intentionally left alone. The companion `hljs` change
  highlights code blocks progressively as they stream rather than only after
  completion.
- **Re-auth from inside the proxy-prefixed UI** — on a proxied node page
  (`/node/{id}/...`), an expiring JWT triggered an in-page login modal whose
  POST went to `/v1/api/auth/login` and was rewritten to
  `/node/{id}/v1/api/auth/login`. Two latent bugs both blocked re-auth: the
  console's `AuthMiddleware` didn't recognise the `/node/{id}/` prefix over a
  public path, and `proxy_api` would have forwarded the login request to the
  upstream node (which mints `JWT_AUD_SERVER` tokens the console then rejects).
  Both fixed: proxied public paths stay public, and `proxy_api` now dispatches
  every entry in `_PROXY_AUTH_LOCAL_HANDLERS` (login, logout, setup, refresh,
  status, whoami, oidc/authorize, oidc/callback) to the console's own auth
  handlers. The dispatch table is a single `(method, path) → handler` mapping
  so the test parametrize list can't drift from the implementation.
- **Appbar visibility + gear-icon dropdown on the dashboard** — the dashboard
  overlay was covering the entire appbar, hiding the proxy-injected node
  picker. The overlay now starts at `top: 48px` and the dashboard's role
  downgrades from `dialog+aria-modal` to `region` so the appbar above it
  remains reachable. The gear icon converts from a direct settings-panel
  click into a dropdown with "MCP connections" and "Logout" (the latter with
  `.destructive` styling). The settings-menu keydown handler is now attached
  synchronously so `Escape` can't fall through the brief window between the
  menu opening and its listeners being installed.
- **PostgreSQL test backend on the notify dispatcher suite** — migration 053's
  `services_notify` trigger lives only in the alembic chain, but the test
  fixture creates tables via `metadata.create_all`. The trigger function +
  trigger are now declared in `_schema.py` and attached via
  `sa.event.listen(services, "after_create", ...)` DDL events gated on the
  PostgreSQL dialect, with the same SQL constants imported by migration 053
  so there's a single source of truth.

## [1.5.12]

### Added

- **Enriched backend error messages** — provider name and attempted URL are now
  included in session error responses, so operators can triage connectivity
  failures without enabling debug logging.

### Fixed

- **`/rewind` always emits a `history` SSE event** — pre-fix, if the session
  had no messages remaining after a rewind the history event was skipped,
  leaving connected UIs with stale content and blocking edit-and-resend flows.

## [1.5.11]

This release introduces one forward-only schema migration:
`052_model_reasoning_persistence` — `surface_persisted_reasoning` and
`replay_reasoning_to_model` flag columns on `model_definitions`.

### Added

- **SSE refresh-resume** — clients that reload mid-stream (browser refresh, tab
  restore) now receive an `in_progress_snapshot` event carrying the buffered
  partial response, so the UI can resume rendering the in-flight turn without
  losing content. The snapshot is keyed by a monotonic `_ws_inflight_seq`
  counter so a reconnecting client can skip events it already saw.
- **Reasoning persistence** (Phases 1–4) — model reasoning text can now be
  persisted to conversation history and optionally replayed to the model on
  subsequent turns. Phase 1 persists reasoning text on the history payload.
  Phase 2 wires a build-time shape filter and a per-model
  `replay_reasoning_to_model` flag. Phases 3+4 add full OpenAI Responses API
  (`include=["reasoning.encrypted_content"]`) and Chat Completions support;
  an `ANTHROPIC_VALID_BLOCK_TYPES` shape filter guards the Anthropic path. Two
  new per-model capability flags (`surface_persisted_reasoning`,
  `replay_reasoning_to_model`) both default `False` on unknown and
  local-server models.
- **Console home composer: placeholders + toggle** — the console landing-page
  composer now shows context-aware placeholder text and a toggle component for
  advanced options; an admin polish pass tightened spacing and focus behaviour
  across the form.

### Changed

- **`judge.model` now requires a named alias** — raw provider model IDs on
  `judge.model` in config are no longer accepted; the judge must reference an
  alias registered in the model registry. The session-provider raw-model
  fallback is removed. Existing configs using an unregistered model ID need a
  corresponding alias entry.

### Fixed

- **`replay_reasoning_to_model` AND-gated with model capability** — setting the
  flag for a model that does not declare reasoning-replay support now silently
  no-ops instead of forwarding reasoning blocks and triggering a provider error.
- **Coordinator alias resolution unified across placeholder + factory** — a
  placeholder coordinator and the real coordinator factory could previously
  resolve to different model aliases, producing a visible mismatch in the model
  display. Both paths now share the same resolution logic.
- **Console `cs=None` fallback in `/v1/api/models` placeholder** — an
  under-initialised coordinator state no longer 500s when the models endpoint
  is hit before the coordinator subsystem is fully bootstrapped.
- **SSE `_ws_inflight_seq` always advances** — sequence numbers were previously
  skipped when an emit was past the buffer cap, leaving gaps in the monotonic
  counter that broke `state_change` / `in_progress_snapshot` ordering on
  reconnect.
- **Reasoning persistence shape + replay fixes** — per-block
  `ANTHROPIC_VALID_BLOCK_TYPES` filter applied; `reasoning_text` is now
  synthesised alongside non-reasoning `provider_blocks` so both appear
  together in the history payload.

## [1.5.10]

This release introduces one forward-only schema migration:
`051_skill_notify_on_complete_array_default` — backfills
`prompt_templates.notify_on_complete` from `'{}'` to `'[]'`.

### Added

- **Skills unlock action** — operators can unlock an installed skill to allow
  local customisation. Once unlocked, the skill's resource content, system
  prompt additions, and notify configuration are editable through the admin UI.
  Skills shipped as part of a bundle remain locked (read-only) until explicitly
  unlocked; the unlock is logged to the audit trail. A lock icon in the
  top-right of the Skills detail pane doubles as the unlock trigger.

### Fixed

- **`skills.sh` install endpoint** — the install script was targeting an
  endpoint removed in an earlier refactor; switched to `/api/download`.
- **Skills `notify_on_complete` default** — the field defaulted to `{}`
  (object) instead of `[]` (array), causing notify configurations to be
  rejected at schema validation.
- **Skills admin UI modal errors** — `.is-visible` class used consistently
  instead of inline `style.display`; stale error text is cleared on submit;
  designer-review lock-icon UX applied.

## [1.5.9]

### Fixed

- **`repair=False` on all display-read `load_messages` call sites** —
  passing `repair=True` on display paths was silently mutating the stored
  message list, causing divergence between what the UI showed and what the
  model received on the next turn.

## [1.5.8]

This release introduces two forward-only schema migrations:
`049_mcp_oauth_schema` — OAuth token + consent tables for MCP servers;
`050_conversations_source_and_reminders` — `_source` and `_reminders` columns
on `conversations`.

### Added

- **MCP OAuth 2.1 + PKCE** — MCP servers that require OAuth can now be
  configured with a client ID and secret through the admin UI. The full token
  lifecycle (acquire → refresh → rotate) is managed automatically; tokens are
  stored encrypted at rest using a key derived from the JWT secret. The consent
  flow runs in-browser via a provider redirect. Rolled out in phases:

  - Minimum admin form and OAuth schema (`21663d15`).
  - Token-at-rest AES-GCM encryption layer (`a4c335d7`).
  - Per-(user, server) OAuth 2.1 + PKCE flow (`b0f7029f`).
  - Per-(user, server) `ClientSession` pool with OAuth dispatch (`1a1043c4`).
  - SDK 401/403 introspection via httpx response hook (`bde09134`).
  - Phase 7 — per-user tool catalog scoping: each user sees only the tools
    their OAuth token is permitted to call (`cfc8a6c8`).
  - Phase 7b — per-user resource + prompt pool dispatch (`b368bdee`).
  - Phase 8 — per-user MCP consent UX: users see a consent dialog on first
    use of an OAuth-gated server and can revoke consent from their profile;
    admins see per-server consent counts in the MCP Servers tab (`61051339`).

- **Metacognition NudgeQueue** — all advisory channels (repeat-tool nudges,
  watch reminders, wake triggers) are unified into a pull-model `NudgeQueue`
  that delivers at most one nudge per turn, preventing multi-channel pile-ups
  that inflate context. Observable changes:

  - Watch results carry metadata (watch ID, `valid_until`, trigger type)
    through to the system message so the model can reason about recency.
  - Coordinator idle-children observer: a coordinator with no in-flight
    children for longer than the configured idle threshold receives a nudge.
  - Wake trigger (`IdleNudgeWatcher`): sessions waiting on an external event
    can be unblocked via `ChatSession.deliver_wake_nudge_from_queue`.
  - Watch switchover: watch results are now enqueued on the `NudgeQueue`
    rather than the previous `_watch_pending` list, giving them the same
    delivery guarantees and priority handling as other advisories.

- **Structured watch-result card** — the UI renders watch results as a styled
  card with a system-nudge marker, distinct from the assistant message body.
  On history replay, system-nudge turns are visually distinguished from normal
  assistant turns.
- **Side-channel persistence** — `_source` and `_reminders` side-channel
  fields are persisted to the `conversations` storage table and restored on
  session resume, so metacognitive context survives process restarts. A
  `REMINDER_TEXT_STORAGE_CAP` byte clamp prevents unbounded growth.

### Fixed

- **Replay consistency** — queued user messages captured mid-loop are now
  persisted and replayed in the correct order on a subsequent `events`
  subscription. Coordinator history replay fixed: blank assistant cards and
  out-of-order tool results on the coordinator tree no longer occur when the
  coordinator has mixed queued + delivered messages.
- **Session reminder preservation on fork + resume** — `_source` and
  `_reminders` are carried through workstream fork and restored from storage
  on resume.
- **NUL-byte sanitization in storage** — PostgreSQL rejects `\x00` in text
  columns; `_source` and `_reminders` now strip NUL bytes on write.
- **Console coordinator subsystem bootstrap** — the coordinator subsystem is
  now committed atomically on first model add; startup teardown is offloaded
  to avoid blocking the event loop.
- **MCP `asyncio.timeout` over `asyncio.wait_for`** — Python 3.11's
  `wait_for` wraps the coroutine in a fresh task, breaking anyio's `aclose`
  scope exit. Replaced with `async with asyncio.timeout(N)` for safe cleanup.
- **MCP pool-reuse 401 recovery** — a reused `ClientSession` returning 401
  now replaces the pool entry with a fresh session; the carrier token is
  owned by the pool entry to prevent a race between the 401 handler and a
  concurrent request.
- **OIDC hardening** — multiple security and correctness fixes:
  SSRF + plaintext credential exfil via discovery document (sec-1, sec-3);
  `TURNSTONE_OIDC_REDIRECT_BASE` now required, Host-header fallback removed
  (sec-2); atomic user + identity provisioning prevents orphan rows (bug-1);
  callback robustness — typed exceptions, shape checks, log sanitization, JS
  race (bug-4–6, sec-4); role-mapping concurrency serialized (bug-2, perf-1);
  stranded-user self-heal on role-mapping failure (cumulative bug-1).

## [1.5.7]

### Added

- **Inline node picker** — a compact node-switcher dropdown in the console
  header replaces the "← Back to console" banner, so operators can switch
  between nodes without a full navigation.

### Fixed

- **Queued user messages injected mid-loop** — messages queued while a
  generation was in progress were not being delivered at the correct seam and
  could be dropped or reordered when the worker consumed the queue.
- **Search tool output bounded** — pathological inputs (very long lines with
  no whitespace) could produce search results exceeding the context budget.
  Output is now clamped before reaching the message.

## [1.5.6]

### Added

- **`api_surface` toggle** — model definitions gain an `api_surface` field
  (`"chat"` | `"responses"`) that selects which OpenAI-compatible API surface
  the provider client uses. Enables Mistral Medium reasoning via the Responses
  surface; Chat Completions remains the default for all other models.
- **Healthy model aliases per node** — `GET /v1/api/cluster/nodes` now
  includes a `healthy_aliases` list per node, so the coordinator and operators
  can see which model aliases are currently reachable without a separate
  per-model health probe.
- **Plan/task agent settings in Models → Roles** — the Models admin tab's
  Roles sub-tab gains `plan_agent` and `task_agent` rows so operators can
  configure per-kind reasoning effort and alias overrides from the UI rather
  than editing `config.toml`. Live-refresh dropdowns update in place when
  model definitions change.

### Fixed

- **Memory candidate selection** — recall now uses OR-of-terms BM25 with
  query-aware candidate-set selection, dramatically improving recall for
  queries whose terms span multiple stored entries.
- **Workstream model + config preserved on rehydrate** — reopening a closed
  workstream no longer overwrites the model alias and per-workstream config
  with session defaults.
- **Console home composer: attachments + user-message pills** — multipart
  attachments in the home composer were not forwarded correctly; user-message
  pills in the coordinator chat pane were missing.

## [1.5.5]

### Fixed

- **Saved-workstream tool result rendering** — tool results in closed
  workstreams were not rendering on history replay. Audit-trail decoration for
  tool calls is now applied on the replay path.

## [1.5.4]

### Added

- **Stage 3 SessionManager Children primitive lift** — child workstreams are
  first-class citizens in the cluster event bus. `child_ws_state` events are
  pushed through the cluster SSE stream so the console tree view updates in
  real time without polling. `list_children` and `get_child` primitives on
  `SessionManager` provide a consistent cross-node view of the coordinator's
  spawn tree.
- **Multi-select delete for Saved Coordinators** — the Saved Coordinators grid
  in the console admin panel now supports checkbox multi-select with a
  bulk-delete action.

## [1.5.3]

This release introduces one forward-only schema migration:
`048_workstream_reaper_index` — partial composite index on `workstreams` for
the orphan-reaper query.

### Fixed

- **Coordinator orphan reaping scoped by heartbeat** — the session manager's
  `close_idle` pass now scopes the DB-orphan reaper by
  `services.last_heartbeat` so workstreams belonging to a live node are not
  incorrectly reaped. `bulk_close_stale_orphans` and `touch_workstream`
  storage primitives added; a partial composite index keeps the reaper scan
  cheap.
- **Coordinator pool idle cleanup** — a periodic task on the console now
  closes coordinator pool entries whose session has gone idle past the
  configurable threshold, preventing pool exhaustion on long-running consoles.

## [1.5.2]

### Added

- **Metacognition themed reminder bubble** — repeat-tool and user-reminder
  nudges are rendered as a distinct styled bubble rather than being injected
  inline into the assistant message, making it easier to distinguish model
  output from metacognitive annotations. The CLI REPL gains matching
  `on_user_reminder` / `on_tool_reminder` callbacks.

### Fixed

- **Metacog streak detector** — the N≥3 sequential-same-call streak detector
  now fires correctly on the third repetition; a write-success-clear that
  reset the counter after a successful tool call (preventing streaks across
  mixed-outcome sequences) was removed.
- **Metacog reminders isolated to side-channel** — reminder text no longer
  appears in the user content turn; it flows through a dedicated side-channel
  the session injects into the system context, preventing the model from
  attributing it to the user.

## [1.5.1]

### Added

- **`pending_approval_detail` on child `ws_state` SSE events** — coordinators
  now receive the child's pending approval detail in `child_ws_state` events,
  enabling the coordinator to surface approval prompts without a separate poll.

### Fixed

- **Coordinator registry auto-refresh** — the console coordinator registry now
  refreshes when model definitions change, so a newly added alias is visible
  to coordinators without restarting.
- **Coordinator fan-out default** — coordinators now fan out to independent
  child workstreams by default instead of serialising them, matching the
  documented contract for parallel-work patterns.
- **`wait_for_workstream` message cap raised to 10 KiB** — large plan
  summaries and tool results from child workstreams were silently truncated at
  the previous 4 KiB cap.
- **Coordinator SSE isolated on dedicated thread pool** — coordinator SSE
  polling now runs on a dedicated 200-thread executor, matching interactive's
  `sse_executor`, so coordinator long-poll blocking no longer contends with
  storage and routing workers on the default pool.

## [1.5.0]

User-visible additions: a unified workstream HTTP surface (interactive and
coordinator under one URL family), inline child approvals, coordinator
composer parity, progressive rendering, OIDC authentication, MCP OAuth
foundations, and a redesigned UI built on the Design System v1 token layer.

This release removes the pre-1.5 body-keyed and query-keyed URL family.
See **Removed (BREAKING)** below before upgrading from a 1.x stable line.

This release introduces the following forward-only schema migrations that the
server applies automatically on first startup. All are additive; no data loss.

- `039_workstream_kind` — `kind` + `parent_ws_id` columns on `workstreams`.
- `040_coord_cluster_admin_perms` — grants `admin.coordinator` +
  `admin.cluster.inspect` to the builtin-admin role.
- `041_workstream_index_tuning` — refined indexes for the workstream query mix
  introduced by 039.
- `042_coord_trust_send_perm` — adds `coordinator.trust.send` permission to
  builtin-admin.
- `043_skill_description_required` — backfills empty `description` rows in
  `prompt_templates`.
- `044_skill_kind` — adds `kind` classifier column to `prompt_templates`
  (`interactive` / `coordinator` / `any`).
- `045_skill_risk_level_rename` — renames `prompt_templates.scan_status` →
  `risk_level`.
- `046_drop_hash_ring_tables` — drops the hash-ring bucket tables superseded
  by rendezvous routing in 1.4.
- `047_drop_coord_spawn_quota_settings` — removes the spawn-quota settings
  rows removed from the coordinator in 1.5.0a4.

### Added

- **Inline child approvals** — pending tool approvals on coordinator child
  workstreams surface directly in the coordinator tree view. A risk pill shows
  the judge verdict (or "pending" while the judge evaluates); Approve/Deny
  buttons appear inline so operators do not need to navigate to the child's
  workstream. `pending_approval_detail` is exposed on
  `GET /v1/api/dashboard` and passed through the cluster live-bulk SSE payload
  so all connected clients render approval prompts simultaneously. LLM judge
  verdicts are cached client-side and replayed on SSE reconnect.
- **Coordinator composer parity** — the coordinator composer now supports
  Stop, Send-to-queue, and Attach (file upload), matching the interactive
  workstream composer feature set.
- **Per-call model and judge override on coordinator composer** — operators
  can override the model alias and judge model for a single coordinator send
  from the composer, without changing the node-wide or role-wide defaults. Bad
  aliases return a corrective error listing available choices.
- **Coordinator status bar + richer history replay** — each coordinator
  workstream gains a per-coordinator status bar showing active children, token
  spend, and generation state. History replay in the coordinator panel is
  extended to include tool results and thinking blocks.
- **Coordinator child error surfacing + memory tool** — child workstream
  errors are surfaced as distinct error rows in the coordinator tree view
  rather than disappearing silently. The coordinator gains access to a
  `memory` tool (same interface as interactive) for retrieving stored facts.
- **Coordinator inline tool-batch construct** — the coordinator tool approval
  UI replaces the separate approval dock with an inline batch construct that
  groups all pending tool calls for a given turn into a single review card.
- **Node capability auto-detection** — nodes report kernel-level capabilities
  (available memory, CPU count, accelerator presence) via
  `/v1/api/node/capabilities` at startup, enabling the console to filter model
  aliases offered to coordinators routing to that node.
- **Skills: paste `SKILL.md` to auto-fill the Create Skill modal** — pasting
  a `SKILL.md` file's content into the modal auto-populates the name,
  description, and configuration fields.
- **Progressive mermaid rendering** — Mermaid diagrams begin rendering as
  soon as a complete diagram block is detected in the stream rather than
  waiting for the full response; the diagram re-renders in place as the model
  extends it.
- **LaTeX and MathML delimiter support** — `\(…\)` inline and `\[…\]` block
  math delimiters are now recognised alongside the existing `$$` fences.

### Removed (BREAKING — 1.5.0)

- **Legacy body-keyed and query-keyed URL family for the workstream
  interaction verbs.** Pre-1.5 interactive shipped both a path-keyed
  and a body-keyed surface for the same five verbs; this release drops
  the body-keyed and query-keyed mounts (and the
  ``make_legacy_body_keyed_adapter`` /
  ``make_legacy_query_keyed_adapter`` shims that backed them). External
  SDK consumers on stable 1.0/1.3/1.4 must move to the path-keyed
  shape:

  | Removed (1.0/1.3/1.4)                          | Use instead                                            |
  | ---------------------------------------------- | ------------------------------------------------------ |
  | ``GET  /v1/api/events?ws_id=X``                | ``GET  /v1/api/workstreams/{ws_id}/events``            |
  | ``POST /v1/api/send``     (body ``ws_id``)     | ``POST /v1/api/workstreams/{ws_id}/send``              |
  | ``DELETE /v1/api/send``   (body ``ws_id``)     | ``DELETE /v1/api/workstreams/{ws_id}/send``            |
  | ``POST /v1/api/approve``  (body ``ws_id``)     | ``POST /v1/api/workstreams/{ws_id}/approve``           |
  | ``POST /v1/api/cancel``   (body ``ws_id``)     | ``POST /v1/api/workstreams/{ws_id}/cancel``            |
  | ``POST /v1/api/workstreams/close`` (body)      | ``POST /v1/api/workstreams/{ws_id}/close``             |

  Calls to the old URLs return **404** on 1.5.0+. Bodies on the new
  URLs no longer carry ``ws_id`` (the path provides it); the
  ``SendRequest`` / ``ApproveRequest`` / ``CancelRequest`` Pydantic
  schemas drop the field, and ``CloseWorkstreamRequest`` slims to a
  single optional ``reason`` field (the body is still required to be
  valid JSON — send ``{}`` when omitting all fields).

  ``/v1/api/plan`` and ``/v1/api/command`` are unaffected and remain
  body-keyed in this release. The bundled web UI, channel adapters,
  Python SDK, TypeScript SDK, and console routing-proxy SDK ship the
  new URLs automatically; pinning to ≥ 1.5.0 is enough.

  The console routing proxy's ``/v1/api/route/...`` family is updated
  alongside: ``/v1/api/route/workstreams/{ws_id}/<verb>`` replaces the
  pre-1.5 ``/v1/api/route/{send,approve,cancel,workstreams/close}``
  mounts. ``DELETE`` is now passed through (``client.request(method,
  ...)`` instead of ``client.post(...)``) so the new dequeue route
  works through the proxy. Audit attribution for ``DELETE`` on
  ``/send`` is logged as ``route.workstream.dequeue`` rather than
  ``route.workstream.send``.

  Auth scope wiring (``WRITE_PATHS`` / ``APPROVE_PATHS`` literals plus
  the path-keyed verb match in ``required_scope``) updated to grant
  ``write`` for path-keyed ``send/cancel/close``, ``approve`` for
  path-keyed ``approve``, and ``write`` for ``DELETE`` on
  path-keyed ``/send``. The ``/node/*`` proxy branch mirrors all four.

### Changed

- **Dashboard row shape: ``id`` → ``ws_id``.** The
  ``GET /v1/api/dashboard`` row dict now keys the workstream
  identifier as ``ws_id`` (matching the rest of the v1 workstream
  surface — active list, saved list, history, detail). The Stage 2
  list-verb lift converged ``/v1/api/workstreams`` and
  ``/v1/api/workstreams/saved`` on ``ws_id`` but left dashboard
  alone to keep that PR's diff focused; this lands the same rename
  on the remaining endpoint so the v1 row shape is consistent
  across the family. Pydantic ``DashboardWorkstream`` and the
  TypeScript SDK ``DashboardWorkstream`` interface both rename the
  field accordingly. The bundled web UI is the only consumer that
  reads ``dashboard.workstreams[].id`` and is updated atomically;
  no external SDK on a stable line reads the field, so the swap is
  bounded by normal static-asset reload. Console
  ``_fetch_live_block`` (cluster-inspect's projection over a
  remote node's dashboard payload) is updated to match.

- **Coordinator gains rich `ws_state` payload + live activity broadcast**
  ([§ Post-P3 reckoning item #2 follow-up]). Pre-lift coord's
  cluster broadcast was state-only — the dashboard's coord rows
  showed the state column flipping but the ``tokens``,
  ``context_ratio``, ``activity``, and per-turn ``content`` fields
  were all hardcoded to zero / empty. The lift turns
  ``on_status`` / ``on_content_token`` / ``on_thinking_start`` /
  ``on_thinking_stop`` / ``on_stream_end`` / ``on_tool_result``
  into shared bodies on :class:`SessionUIBase` so coord populates
  the same per-ws metric fields interactive does (the fields were
  already declared on the base; only the writes were
  WebUI-specific). ``coord_adapter.emit_state`` now reads the UI's
  snapshot under ``_ws_lock`` via the new
  :meth:`SessionUIBase.snapshot_and_consume_state_payload` helper
  and passes the rich kwargs through to
  ``collector.emit_console_ws_state``; the cluster dashboard's
  coord rows now render with the same tokens / activity / content /
  context_ratio fields interactive rows do.

  Three observable behaviour changes (all CHANGELOG-callout-worthy):

  - **Coord persists ``usage_event`` storage rows.** Pre-lift only
    WebUI did. The lifted ``on_status`` body unifies usage tracking
    so governance dashboards / token-spend queries see coordinator
    consumption alongside interactive. Operators querying
    ``usage_event`` by ``ws_id`` will see coord rows for the first
    time.
  - **Coord broadcasts live activity transitions.** New
    ``ClusterCollector.update_console_ws_activity(ws_id, *,
    activity, activity_state)`` method (named ``update_*`` rather
    than ``emit_*`` to flag the no-fan-out asymmetry vs. the rest
    of the ``emit_console_ws_*`` family — it updates the in-memory
    pseudo-node row but intentionally does NOT fan out a separate
    SSE event). The cluster dashboard's per-ws polling reads the
    in-memory pseudo-node row, so activity ticks land on the next
    snapshot fetch (matches WebUI's behaviour where activity
    events are observational; not fanned out through the cluster
    SSE stream).
  - **Cluster ``cluster_state`` events for coord rows now carry
    non-zero ``tokens`` / ``content`` fields.** Frontend rendering
    that conditionally hid these on coord rows can drop the
    branch.

  Architecture changes:

  - ``_MAX_TURN_CONTENT_CHARS`` moved from ``turnstone.server`` to
    ``turnstone.core.session_ui_base`` so coord enforces the same
    per-turn content cap interactive does.
  - WebUI keeps ``on_status`` / ``on_tool_result`` / ``on_error``
    overrides that layer Prometheus ``_metrics.record_*`` calls
    (node-only) on top of the shared body via ``super()`` — the
    Prometheus surface stays node-scoped (the console isn't a
    node and has no /metrics endpoint).
  - ``ConsoleCoordinatorUI`` adds a ``_broadcast_activity``
    override that fans out via the cluster collector instead of
    the global SSE queue (which is node-only on interactive).
  - ``coord_endpoint_config`` wires a new ``_coord_spawn_metrics``
    hook (mirrors interactive's) so the per-spawn ``_ws_messages``
    increment + ``_ws_turn_tool_calls`` reset happen on coord too.

  Test additions: 23 new tests in ``tests/test_coord_rich_ws_state_payload.py``
  pin the per-ws metric writes (status, content accumulation,
  activity tracking, tool-result counters, stream-end activity
  clear), the snapshot helper's IDLE/ERROR drain semantics +
  single-lock-acquisition guarantee, the adapter's rich-payload
  pass-through + defensive None-UI handling, the activity
  broadcast (collector wire + failure swallow + no-op-when-
  collector-unset + dedup against last-emitted state), the
  spawn_metrics hook, and a concurrent-writes-during-snapshot
  stress case (cycles through running / idle / error so the
  drain branches actually run against a concurrent writer).
  Plus WebUI-override regression tests confirming
  ``_metrics.record_*`` still fires on top of the lifted bodies.
  Existing ``tests/test_webui_content.py`` updated to import
  ``_MAX_TURN_CONTENT_CHARS`` from its new home in
  ``turnstone.core.session_ui_base``;
  ``tests/test_coordinator_adapter.py`` updated to expect the
  rich-payload kwargs (``tokens=0`` defaults) on
  ``emit_console_ws_state``.

  Two deferred follow-ups (out-of-scope for this lift,
  flagged for tracking):

  - **Synchronous ``record_usage_event`` INSERT on coord worker
    thread.** The lifted ``on_status`` body persists usage rows on
    every provider response — same shape WebUI uses, but coord
    workers can fire multi-step plan/task agent loops where each
    response blocks the worker for a write transaction. Parity
    with WebUI is the explicit goal here; if coord throughput
    becomes a concern, batch usage_event writes onto a background
    flusher thread (one batch INSERT per N events / per K ms) on
    both kinds.
  - **Coord assistant turn content now flows on the cluster SSE
    stream (``/v1/api/cluster/events``).** Pre-lift the broadcast
    was ``content=""``; post-lift it carries the joined assistant
    output. The cluster SSE stream has no per-user filter today —
    extends an existing cross-tenant exposure (interactive
    ``cluster_state`` events already carry content) to a
    previously-empty channel (coord rows). Proper fix needs the
    SSE endpoint gated on ``admin.cluster.inspect`` (matching
    ``/v1/api/cluster/ws/{ws_id}/detail``) or per-listener
    user_id filtering. Tracked as a separate security-tightening
    project; not gating this lift since it inherits an existing
    exposure rather than introducing a new mechanism.

- **`history` / `detail` verb bodies lifted across both kinds**
  ([Stage 2 Verb Lift — `history` / `detail`]). The coord
  ``GET /v1/api/workstreams/{ws_id}/history`` and
  ``GET /v1/api/workstreams/{ws_id}`` handlers now share two factory
  bodies via ``make_history_handler(cfg)`` and
  ``make_detail_handler(cfg)``. The lift adds both endpoints to the
  interactive surface as a feature gain (pre-lift only coord exposed
  them; interactive consumers had to subscribe to ``/events`` SSE
  just to read history rows or display fields). No new
  ``SessionEndpointConfig`` fields — the factories reuse
  ``permission_gate``, ``manager_lookup``, ``not_found_label``,
  ``audit_action_prefix``, and (for history's storage-fallback kind
  check) ``list_kind`` — all already wired by both production
  lifespans.

  Three observable behaviour changes (all documented per kind):

  - **Interactive gains ``GET /v1/api/workstreams/{ws_id}``.** Pre-lift
    interactive had no detail endpoint — SDK consumers had to read
    display fields from the SSE replay on ``/events`` or scrape the
    active list. The lifted body lazy-rehydrates a closed/evicted
    workstream via ``mgr.open()`` so the response shape is stable
    across loaded / persisted-only states. Same
    ``{ws_id, name, state, user_id, kind}`` shape coord exposed
    pre-lift, now available on both surfaces.
  - **Interactive gains ``GET /v1/api/workstreams/{ws_id}/history``.**
    Same ``?limit=`` query param contract as coord (default 100, max
    500, malformed values fall back to 100, out-of-range clamps to
    [1, 500]). Persisted-but-not-loaded interactives serve history
    without rehydrating — the lifted body falls back to a storage-row
    + kind check (via ``cfg.list_kind``) when ``mgr.get`` returns
    ``None``, mirroring coord's pre-lift
    ``_resolve_coordinator_or_404`` ladder.
  - **Storage / manager-lock work moved off the event loop on coord.**
    The lifted ``history`` body always runs ``storage.get_workstream``
    (storage-fallback path) and ``storage.load_messages`` through
    ``asyncio.to_thread``; pre-lift coord ran them inline on the
    event loop. Long-tail message reads on a saturated console no
    longer stall every other async handler for the duration of the
    SQL.

  Pydantic schemas: ``CoordinatorDetailResponse`` and
  ``CoordinatorHistoryResponse`` removed; both folded into
  ``WorkstreamDetailResponse`` / ``WorkstreamHistoryResponse`` on
  the shared ``server_schemas.py`` (mirrors the list lift's pattern
  for ``WorkstreamInfo``). Both server and console OpenAPI specs
  reference the unified schemas; ``server_spec.py`` gains
  ``EndpointSpec`` entries for the new interactive endpoints. TS
  SDK gains ``WorkstreamDetailResponse`` / ``WorkstreamHistoryResponse``
  interfaces in ``sdk/typescript/src/types.ts``;
  ``openapi-{server,console}.json`` regenerated.
  ``GET /v1/api/workstreams/{ws_id}/history`` is the only verb
  whose lifted body keeps a kind-aware storage fallback (via
  ``cfg.list_kind``); ``detail`` defers cross-kind isolation to
  ``mgr.open()`` itself.

- **`list` / `saved` verb bodies lifted across both kinds** ([Stage 2
  Verb Lift — `list` / `saved`]). The interactive
  ``GET /v1/api/workstreams`` + ``GET /v1/api/workstreams/saved``
  and coord ``GET /v1/api/workstreams`` + ``GET /v1/api/workstreams/saved``
  handlers now share two factory bodies via
  ``make_list_handler(cfg)`` and ``make_saved_handler(cfg)``. Four
  new ``SessionEndpointConfig`` fields capture the per-kind
  divergence:

  - ``list_resolve_titles: ListResolveTitles | None`` — interactive
    wires :func:`turnstone.core.memory.get_workstream_display_names`
    (new bulk helper added on the storage layer + ``memory.py``)
    so the active-list endpoint resolves every user-set alias in
    ONE ``SELECT ... WHERE ws_id IN (...)`` instead of the pre-lift
    per-row N+1. Coord wires ``None`` (no alias surface today).
  - ``list_kind: WorkstreamKind | None`` — required storage-side
    kind classifier passed to ``list_workstreams_with_history``.
    Interactive wires ``WorkstreamKind.INTERACTIVE``; coord wires
    ``WorkstreamKind.COORDINATOR``. Distinct from
    ``audit_action_prefix`` (audit-action namespacing) so adding a
    third kind doesn't have to overload the audit prefix as a
    classifier; missing value surfaces as 500 with a clear log
    line rather than silently filtering for the wrong kind.
  - ``saved_state_filter: str | None`` — coord wires ``"closed"``
    so only explicitly-closed coordinators surface in the
    saved-card grid. Interactive wires ``None`` (the storage
    layer already excludes ``state='deleted'`` tombstones).
  - ``saved_loaded_lookup: SavedLoadedLookup | None`` — coord-only
    defence-in-depth filter that excludes ws_ids currently in the
    in-memory pool (a row can be ``state='closed'`` for a few
    seconds while the close-emit sequence races the in-memory pop).
    Interactive wires ``None``.

  Five observable behaviour changes (all documented per kind):

  - **Active-list top-level key converges on ``"workstreams"``.**
    Pre-lift coord returned ``{"coordinators": [...]}``; the lifted
    body returns ``{"workstreams": [...]}`` for response-shape
    parity with interactive. Coord is a 1.5.0aN-only surface — never
    shipped stable — so SDK / frontend consumers swap once and
    there's no compat shim or fallback (the convergence MUST land
    before v1.5.0 stable per
    ``project_unification_before_stable.md``).
  - **Saved-list top-level key converges on ``"workstreams"``.**
    Same shape change as the active list, applied to
    ``GET /v1/api/workstreams/saved`` on coord. Coord-only surface;
    no compat shim.
  - **Active-list row key renames ``"id"`` → ``"ws_id"``** on
    interactive. Pre-lift interactive used the bare ``id`` field
    while every other shared verb on this surface (cancel, open,
    events, create, saved-list) uses ``ws_id``. Convergence
    eliminates the internal inconsistency. Frontend consumers
    reading ``ws.id`` from the active-list response swap to
    ``ws.ws_id``. Interactive HAS shipped stable across 1.0 / 1.3 /
    1.4, but the active-list endpoint is consumed by the bundled
    JS only — there's no external SDK on those stable lines reading
    the field. Browser-cache staleness is bounded by normal
    static-asset reload on next page load.
  - **Active-list row gains always-include fields.** ``user_id``
    was coord-only; ``kind`` + ``parent_ws_id`` were
    interactive-only. Both kinds now populate all three.
    ``parent_ws_id`` defaults to ``None`` for coord (coordinators
    have no parent).
  - **Storage / manager-lock work moved off the event loop on
    interactive.** The lifted ``saved`` body always uses
    ``asyncio.to_thread`` for ``list_workstreams_with_history``;
    pre-lift interactive ran it inline (correlated COUNT subquery
    can stall every other async handler on a cluster with thousands
    of saved rows). Coord already used ``to_thread`` (perf-2 from
    the saved-coordinators review); convergence lifts interactive
    up. The active-list body also moves ``mgr.list_all`` +
    per-row title resolution off the event loop on both kinds.

  Pydantic schemas: ``WorkstreamInfo.id`` renamed → ``ws_id``,
  ``WorkstreamInfo.user_id`` field added. ``CoordinatorInfo`` and
  ``CoordinatorListResponse`` removed (folded into the unified
  ``WorkstreamInfo`` / ``ListWorkstreamsResponse``); ``console_spec``
  active-list endpoint now points at ``ListWorkstreamsResponse``.
  OpenAPI spec snapshots regenerated.

  ``GET /v1/api/dashboard`` is **not** in the lift's scope and
  still returns rows keyed on ``id``. A separate cleanup PR will
  converge the dashboard row shape with the rest of the v1 surface.

- **`SessionManager.create` gains a deferred-emit option; lifted
  ``create`` HTTP handler eliminates the phantom create→close
  pair on coord rollback.** ``SessionManager.create`` now accepts
  ``defer_emit_created: bool = False`` (default preserves the
  legacy "advertise immediately" contract for direct callers); two
  new methods complete the deferred-create bracket:
  - ``SessionManager.commit_create(ws)`` fires the deferred
    ``emit_created`` event after the caller's post-create work
    confirms the workstream should be advertised.
  - ``SessionManager.discard(ws_id)`` releases the in-memory slot
    + cleans up the UI WITHOUT firing ``emit_closed`` — the
    workstream's existence was never advertised, so there's
    nothing to advertise on rollback. Storage-row deletion stays
    a separate concern (caller invokes ``delete_workstream`` for
    a complete rollback), mirroring ``mgr.create``'s split between
    slot reservation and ``register_workstream``. Logs a
    ``warning`` (``session_mgr.discard.after_emit_created``) when
    invoked on a workstream that's already been advertised
    (non-deferred create or post-``commit_create``); the slot is
    still released so capacity isn't stranded, but the warning
    surfaces the caller-bug case where ``close`` would have been
    the right call.

  The lifted ``make_create_handler`` now uses this bracket: pass
  ``defer_emit_created=True``, validate uploaded attachments, then
  ``mgr.commit_create(ws)`` on success / ``mgr.discard(ws.id)`` on
  failure. Pre-fix, coord's ``mgr.create`` fired ``emit_created``
  synchronously — a rollback then called ``mgr.close`` which
  fired ``emit_closed``, surfacing a quick create→close pair on
  the cluster events stream that the collector's diff-reconcile
  had to handle. Post-fix, a rejected upload produces zero
  events. Interactive's ``emit_created`` is a documented no-op
  stub so the deferral is observably a no-op there; the
  ``ws_created`` broadcast on the global SSE queue continues to
  fire from the kind's post_install callback after attachment
  validation passes (unchanged).

  Direct callers of ``mgr.create`` (test fixtures, the CLI REPL,
  channel adapters) keep the default ``defer_emit_created=False``
  and see no behaviour change.

- **Coordinator HTTP surface unified under `/v1/api/workstreams/`**
  ([Stage 2 Priority 0]). The experimental `/v1/api/coordinator/*`
  URL tree from 1.5.0aN is removed; coord verbs now mount at the
  same shape as interactive workstreams via a shared route
  registrar (`turnstone.core.session_routes`). Path mapping:

  | Was (1.5.0aN)                                    | Now                                              |
  |--------------------------------------------------|--------------------------------------------------|
  | `POST /v1/api/coordinator/new`                   | `POST /v1/api/workstreams/new`                   |
  | `GET  /v1/api/coordinator`                       | `GET  /v1/api/workstreams`                       |
  | `GET  /v1/api/coordinator/saved`                 | `GET  /v1/api/workstreams/saved`                 |
  | `GET  /v1/api/coordinator/{ws_id}`               | `GET  /v1/api/workstreams/{ws_id}`               |
  | `POST /v1/api/coordinator/{ws_id}/{verb}`        | `POST /v1/api/workstreams/{ws_id}/{verb}`        |

  Permission scopes, request / response bodies, and SSE event shapes
  are unchanged. Callers on the experimental 1.5.0aN coord SDK must
  swap their URL prefix; the legacy paths are gone with no compat
  shim. Stable releases (1.0 / 1.3 / 1.4) never exposed
  `/v1/api/coordinator/`, so this change is a no-op for anyone
  upgrading from a stable line.

  Two handler bodies (`approve`, `close`) lifted into the shared
  registrar with kind branching behind `SessionEndpointConfig` —
  both kinds share one implementation per verb. Two related
  behavior changes on the interactive close path:

  - `mgr.close()` race-loss returns 404 (was 500 on coord;
    "popped between .get() and .close()" is a not-found semantic,
    not a server error).
  - Audit-write failures (`record_audit` raising on the storage
    write) are now caught and logged at `warning` level; the close
    still returns 200. Previously the interactive path let the
    exception propagate as HTTP 500. Coord previously already
    swallowed; convergence is intentional — operators monitor the
    `ws.close.audit_failed` log line in both kinds the same way.

  Other shared verbs (`send`, `cancel`, `open`, `events`, `create`,
  `list`, `saved`, `history`, `detail`) keep their per-kind
  handlers — body convergence for those requires SessionManager-
  side refactors (e.g. Priority 1's worker-dispatch unification
  for `send`) or coordinated frontend changes (response-shape
  unification for `list` / `saved`) that fall outside Priority 0
  scope.

- **TypeScript SDK bumped to 0.4.0** to flag the URL change for any
  1.5.0aN-era consumer of the experimental coord client. The
  `openapi-{server,console}.json` reference specs ship with the
  unified path tree.

- **Worker dispatch unified across interactive + coordinator**
  ([Stage 2 Priority 1]). The atomic check-and-(spawn-or-queue)
  decision for ``ChatSession.send`` now lives in
  ``turnstone.core.session_worker.send`` and is shared by both
  paths. Interactive ``/v1/api/send``, the coordinator adapter, the
  watch-result dispatch, the rewind/retry path, and the
  initial-message-on-create path all gate on
  ``Workstream._worker_running`` (set/cleared atomically under
  ``ws._lock``) instead of ``Thread.is_alive()`` — closes a race
  where two senders could spawn parallel workers on the same
  ChatSession.

  The ``/send`` HTTP body itself stays per-kind in this PR.
  Verb-shape convergence (one shared factory body with capability
  flags for attachments / queue priorities / metric increments) is
  tracked as P1.5 and MUST land before 1.5.0 stable — letting the
  fork ship into the stable line bakes the duplication in for the
  lifetime of the 1.5 track.

- **`/send` body lift + coordinator attachments + queue surface
  parity** ([Stage 2 Priority 1.5]). The ``/send`` HTTP handler is
  now ONE factory body (``make_send_handler(cfg)``) wired with
  capability flags on both kinds; the four attachment endpoints
  (``upload`` / ``list`` / ``get_content`` / ``delete``) are also
  unified via ``make_attachment_handlers(cfg)``. Coord workstreams
  light up:

  - ``POST/GET /v1/api/workstreams/{ws_id}/attachments``,
    ``GET .../attachments/{aid}/content``,
    ``DELETE .../attachments/{aid}`` — same shape, same caps, same
    reservation flow as interactive.
  - ``POST /v1/api/workstreams/{ws_id}/send`` accepts
    ``attachment_ids`` (or auto-consumes pending) and returns
    ``attached_ids`` / ``dropped_attachment_ids`` for surfacing
    partial reservations. Live-worker reuse path also returns
    ``priority`` / ``msg_id`` (parity with the interactive
    ``status: queued`` shape).

  Backend parity is end-to-end: storage layer was already
  kind-agnostic; the route registrar's ``AttachmentHandlers`` slot
  has been there since Stage 2 P0; the multi-node attachment
  routing-proxy on the console (``route_attachment_proxy``) was
  already shipping. P1.5 is the wiring + verb-shape lift that lets
  these primitives surface on the coord side.

  Coord dashboard rendering surfaces an attachment-count badge on
  past messages with attachments; full chip rendering with
  click-to-view is deferred (the coord dashboard is
  diagnostic-leaning and chip parity isn't on the critical path
  for the unification thesis). Python SDK adds
  ``coordinator_send`` / ``coordinator_upload_attachment`` /
  ``coordinator_list_attachments`` /
  ``coordinator_get_attachment_content`` /
  ``coordinator_delete_attachment`` on
  ``AsyncTurnstoneConsole`` + ``TurnstoneConsole``. TS SDK
  regenerated; bumped to 0.5.0.

  Three lifted helpers (``sniff_image_mime``,
  ``classify_text_attachment``, ``upload_lock``) moved from
  ``turnstone/server.py`` to ``turnstone/core/attachments.py`` so
  both processes use the canonical implementation. The interactive
  surface keeps the same behaviour; the helpers are simply
  imported from their new home.

  ``coordinator_send`` no longer returns ``429`` on a full worker
  queue — the unified body returns ``200 {"status": "queue_full"}``
  for parity with interactive. Existing callers checking for ``429``
  should switch to the status-code shape.

  Coord ``GenerationCancelled`` now emits ``state=idle`` +
  ``stream_end`` (parity with interactive); pre-P1.5 a cancel-killed
  coord worker would have terminated silently with no state event.
  Cluster fanout / alerting keyed on ``state=error`` for cancelled
  coord workers should switch to monitoring ``stream_end`` /
  ``state=idle`` together.

- **`SessionKindAdapter` Protocol split into construction +
  emission** ([Stage 2 Priority 3]). The adapter Protocol now covers
  only what every kind must implement (``kind`` / ``build_ui`` /
  ``build_session`` / ``cleanup_ui``); the four lifecycle emit
  methods (``emit_created`` / ``emit_state`` / ``emit_rehydrated`` /
  ``emit_closed``) move to a separate ``SessionEventEmitter``
  Protocol wired through a new optional
  ``event_emitter: SessionEventEmitter | None`` kwarg on
  ``SessionManager``. Both production adapters (interactive on
  ``server.py``, coordinator on ``console/server.py``) implement
  both Protocols and are passed as both ``adapter`` and
  ``event_emitter`` at lifespan-construction time, so production
  behavior is unchanged. The interactive adapter's three
  ``emit_created`` / ``emit_state`` / ``emit_rehydrated`` methods
  remain documented no-op stubs (those events fire from out-of-band
  paths — the create handler enqueues ``ws_created`` after
  attachment validation, ``WebUI._broadcast_state`` emits
  ``ws_state``); ``emit_closed`` stays load-bearing as the sole
  transport path for ``ws_closed`` onto the global SSE queue.

- **`cancel` verb body lifted across both kinds** ([Stage 2 Verb
  Lift — `cancel`]). The interactive ``/v1/api/cancel`` and coord
  ``/v1/api/workstreams/{ws_id}/cancel`` handlers now share one
  body via ``make_cancel_handler(cfg, *, audit_emit=None)``;
  per-kind divergence captured by a new
  ``cancel_forensics: CancelForensics | None`` field on
  ``SessionEndpointConfig`` (interactive wires
  ``_capture_cancel_forensics``; coord wires ``None``).
  Three observable behaviour changes for coord callers:

  - **Coord cancel now accepts a ``force`` flag.** Same shape as
    interactive: posting ``{"force": true}`` abandons the worker
    thread and emits ``stream_end`` so a stuck coord generation
    can be recovered without waiting for the daemon thread to
    exit. Pre-lift coord ignored ``force``.
  - **Coord cancel response always includes ``"dropped"``.**
    Pre-lift coord returned bare ``{"status": "ok"}``; the lifted
    body returns ``{"status": "ok", "dropped": {}}`` (always-include
    parity with interactive). SDK consumers don't need to branch
    on kind to read ``dropped``.
  - **Coord cancel returns 400 when the workstream's session is
    ``None``.** Pre-lift coord called ``coord_mgr.cancel`` which
    silently no-op'd on a placeholder/build-failed workstream; the
    lifted body 400s with ``{"error": "No session"}`` for parity
    with interactive's pre-existing branch.

  Two observable changes for interactive (asymmetric — coord
  pre-lift already had this behaviour):

  - ``resolve_plan`` now runs on every cancel (previously gated
    on ``was_running``). ``resolve_plan`` has an internal
    ``_pending_plan_review is None`` guard, so the call is no-op
    when no plan review is pending. Lift gives interactive coord's
    pre-lift recovery path: a stuck plan-pending state from a
    crashed worker can be cleared via ``cancel`` instead of
    requiring a workstream close + rehydrate.
  - ``resolve_approval`` runs on every cancel **only when
    ``ui._pending_approval is not None``** (the lifted body gates
    the call). ``resolve_approval`` is not idempotent — it always
    broadcasts ``approval_resolved`` and overwrites
    ``_approval_result`` — so the gate prevents a stale resolution
    event from leaking on idle cancels while preserving the recovery
    path when an approval really is pending.

  Coord ``coordinator.cancel`` audit detail now includes ``force``
  so operator-driven recovery is distinguishable from a routine
  cancel in the audit log.

  Three /review fixes folded into the same commit:

  - **No more stale ``approval_resolved`` SSE event on idle cancel.**
    The lifted body's ``resolve_approval`` call is now gated on
    ``ui._pending_approval is not None``. Pre-fix, the unconditional
    call would broadcast a phantom ``approval_resolved`` to every
    SSE listener even when no prompt was pending — listener UIs
    that key on the event would dismiss prompts they didn't have.
  - **Force-cancel now clears ``_worker_running`` alongside
    ``worker_thread``.** Previously the force path left the half-
    state ``(_worker_running=True, worker_thread=None)``, which
    routed any follow-up ``send`` through the queue-enqueue path
    onto the abandoned worker (where the cancel flag short-circuits
    the queue-drain seam, leaving the message orphaned until the
    next spawn). Restores the
    ``(worker_thread, _worker_running)`` invariant
    ``session_worker.send`` documents.
  - **``coordinator_stop_cascade`` now treats child cancel
    ``400 + "No session"`` as ``skipped``** (was previously
    ``failed``). Lifted coord cancel returns 400 on placeholder /
    build-failed children — matching the pre-lift outcome where
    those children were silently no-op'd, so the cascade response's
    ``failed`` bucket no longer fires spurious operator alerts.

- **`open` verb body lifted across both kinds** ([Stage 2 Verb
  Lift — `open`]). The interactive
  ``POST /v1/api/workstreams/{ws_id}/open`` and coord
  ``POST /v1/api/workstreams/{ws_id}/open`` handlers now share one
  body via ``make_open_handler(cfg, *, audit_emit=None)``. Per-kind
  divergence captured by two new ``SessionEndpointConfig`` fields:

  - ``open_resolve_alias: AliasResolver | None`` — interactive
    wires :func:`turnstone.core.memory.resolve_workstream` so
    callers can pass user-friendly aliases ("my-debug-ws") in the
    path param. Coord wires ``None`` (hex ids only).
  - ``open_post_load: OpenPostLoad | None`` — interactive wires the
    UI-replay (``clear_ui`` + history) + handler-side ``ws_created``
    enqueue onto the global SSE queue. Coord wires ``None`` and
    relies on the cluster collector fan-out from
    ``CoordinatorAdapter.emit_rehydrated``.

  **Load-bearing fix** (§ Post-P3 reckoning item #3): interactive
  ``open_workstream`` previously called
  ``mgr.create(ws_id=resolved_id)`` + ``ws.session.resume(...)`` to
  rehydrate, bypassing ``mgr.open()`` entirely. After the lift both
  kinds route through ``mgr.open()`` — which makes
  ``InteractiveAdapter.emit_rehydrated`` reachable on interactive
  (it had been dead-by-routing) and gives the manager a single
  rehydrate code path to maintain. ``emit_rehydrated`` stays a
  documented no-op stub on the interactive adapter (the
  handler-side ``ws_created`` enqueue from ``open_post_load`` is
  the load-bearing emission).

  Two observable behaviour changes for interactive callers:

  - **Cross-kind open returns 404** (was 400). Pre-lift had a
    pre-mgr storage probe that returned ``400`` with
    ``"Workstream is not an interactive kind"`` for coord rows;
    the lift consolidates on ``mgr.open()``'s single ``None``-
    return contract for missing / wrong-kind / tombstoned rows.
    Security boundary unchanged.
  - **Already-loaded response uses ``ws.name`` directly** (was
    ``get_workstream_display_name(resolved_id) or resolved_id``).
    A workstream renamed via ``set_workstream_alias`` after being
    loaded into memory will surface the storage-row name in the
    open response's ``name`` field instead of the latest alias.
    The dashboard listing endpoint still resolves aliases on its
    own pass, so the user-visible workstream name in the tab strip
    isn't affected.

  Coord behaviour unchanged.

  Two /review fixes folded into the same commit:

  - **Resume failures now return 5xx instead of broken-200.**
    ``SessionManager.open()`` previously caught and ``log.debug``-
    swallowed exceptions from ``ChatSession.resume`` (which assigns
    ``self.messages`` *before* the config-restore block, so a
    partial-failure resume — corrupted ``workstream_config`` row,
    model-registry mismatch on a saved alias, malformed
    ``temperature`` / ``max_tokens`` — would leave the session with
    history but with default config). Pre-lift, the interactive
    open handler called ``ws.session.resume(...)`` directly and let
    exceptions propagate as 500. The lift accidentally inherited
    the swallow because it routed through ``mgr.open()``. Restored
    pre-lift behaviour: ``mgr.open()`` now re-raises resume
    exceptions after rolling back the slot (``cleanup_ui`` +
    ``_remove_locked``), so the lifted handler returns 500 with
    a correlation id and the storage row stays available for a
    retry instead of silently 200'ing with broken state.
  - **``except Exception`` in the lifted body documents intent.**
    The bare exception catch around ``mgr.open(ws_id)`` is
    intentional — the kind's session factory has no documented
    exception spec, and resume can propagate from
    ``ChatSession.resume``. A one-line rationale comment in the
    handler body keeps a future contributor from narrowing it
    incorrectly.

- **`events` verb body lifted across both kinds** ([Stage 2 Verb
  Lift — `events`]). The interactive
  ``GET /v1/api/events?ws_id=...`` and coord
  ``GET /v1/api/workstreams/{ws_id}/events`` SSE handlers now
  share one body via ``make_events_handler(cfg)``. Per-kind
  divergence captured by a new
  ``events_replay: EventsReplay | None`` cfg field — a Protocol-
  typed callback yielding the kind-specific initial replay
  payload that the lifted body iterates and sends as ``data:``
  lines before starting the live event loop. Interactive's
  ``_interactive_events_replay`` yields the pre-lift sequence
  (``connected`` + ``status`` + ``history`` + ``pending_approval``
  + cached intent verdicts + ``pending_plan_review``); coord's
  ``_coord_events_replay`` yields just ``pending_approval`` +
  ``pending_plan_review`` (matches pre-lift coord behaviour).

  The legacy interactive query-keyed URL is preserved via a new
  ``make_legacy_query_keyed_adapter`` helper (sister to
  ``make_legacy_body_keyed_adapter`` from earlier lifts) — it
  reads ``ws_id`` from the query string and splices into
  ``request.path_params`` before delegating to the lifted body.
  ``GET /v1/api/events?ws_id=...`` continues to work for any 1.x
  SDK consumer.

  Two convergence wins:

  - **Coord gains SSE connect/disconnect metrics.** Pre-lift
    coord didn't record per-stream metrics; the lifted body
    always calls ``metrics.record_sse_connect()`` /
    ``...disconnect()``, giving the cluster dashboard the same
    per-stream observability interactive's had since 1.0.
  - **Both kinds now check ``request.is_disconnected()`` AND
    the ``ws_closed`` event** to terminate. Pre-lift interactive
    relied solely on ``ws_closed`` (which never fires if the
    client just goes away without closing the workstream);
    pre-lift coord relied solely on ``is_disconnected``. The
    lifted body uses both — whichever fires first wins.

  One observable shape change for coord callers: the lifted body
  returns 409 ``"session has no UI"`` when ``ws.ui`` is missing
  (placeholder / build-failed UI), matching pre-lift coord.
  Pre-lift interactive returned 404 in this case; the lift
  converges on 409 across kinds because the workstream EXISTS
  (404 would imply it doesn't).

  **Item #2 from § Post-P3 reckoning split out** of this lift
  during scoping (rich ``ws_state`` payload parity for coord —
  lifting coord's ``ConsoleCoordinatorUI`` to broadcast
  ``tokens + context_ratio + activity + content`` like
  ``WebUI._broadcast_state`` does). The body lift touches
  ``session_routes.py`` + ``server.py`` + ``console/server.py``;
  the rich-payload work touches ``coordinator_ui.py`` +
  ``collector.py`` + ``session_ui_base.py`` (different files,
  different reviewer concern). Tracked as standalone follow-up
  ``feat/coord-rich-ws-state-payload``.

  Two /review fixes folded into the same commit:

  - **Restored interactive's dedicated SSE thread pool.** The
    initial draft of ``make_events_handler`` used
    ``asyncio.to_thread`` (default executor, capped at
    ``min(32, cpu_count + 4)``) for the per-connection
    ``client_queue.get`` blocking wait. Pre-lift interactive used
    a dedicated 200-thread ``sse_executor`` (created in the
    lifespan with ``thread_name_prefix="sse"``) precisely to
    avoid this — under high concurrent SSE counts the default
    pool starves and SSE polling contends with every other
    ``asyncio.to_thread`` caller in the process (storage, router,
    audit). Restored isolation via a new
    ``sse_executor_lookup: SseExecutorLookup | None`` cfg field;
    interactive returns ``request.app.state.sse_executor``, coord
    wires ``None`` and falls through to the default executor.
  - **Restored 5s queue.get poll** (was shortened to 1s in the
    initial draft). The 5x wakeup-rate bump compounded the thread-
    pool starvation; the ``request.is_disconnected()`` probe
    between polls already covers cancel-detection latency the
    timeout would otherwise gate.
  - **Replay phase streams events directly from the generator
    instead of pre-building into a list.** The initial draft
    materialised the entire kind-specific replay payload
    (``connected`` + ``status`` + ``history`` + pending prompts)
    into a list before constructing the ``EventSourceResponse``,
    delaying time-to-first-byte until the heaviest replay event
    (``_build_history`` for long-running interactive workstreams)
    finished serialising AND letting the per-UI listener queue
    accumulate over its 500-slot cap on a chatty mid-generation
    workstream. The lifted body now iterates ``cfg.events_replay``
    inside the async generator so each event ships as soon as the
    callback yields it; the existing observational-failure swallow
    semantics are preserved by wrapping the iteration in the same
    try/except.

- **`create` verb body lifted across both kinds** ([Stage 2 Verb
  Lift — `create`]). The interactive
  ``POST /v1/api/workstreams/new`` and coord
  ``POST /v1/api/workstreams/new`` handlers now share one body via
  ``make_create_handler(cfg, *, audit_emit=None)``. Per-kind
  divergence captured by five new ``SessionEndpointConfig`` fields:

  - ``create_supports_attachments: bool`` — multipart body parsing
    + attachment validation+save+rollback. Both kinds wire ``True``.
  - ``create_supports_user_id_override: bool`` — trusted-source
    body ``user_id`` override (interactive ``True`` for console-
    proxied creates; coord ``False``).
  - ``create_validate_request: CreateRequestValidator | None`` —
    per-kind pre-create gates (interactive: ws_id format, kind,
    parent ownership, attachments+resume_ws combo; coord: 401-on-
    empty-uid).
  - ``create_build_kwargs: CreateKwargsBuilder | None`` — per-kind
    kwargs dict for ``mgr.create``.
  - ``create_post_install: CreatePostInstall | None`` — per-kind
    tail end (interactive: WebUI auto_approve + watch_runner +
    ``ws_created`` global broadcast + atomic resume + skill session
    config + notify_targets + routing override + initial-message
    worker thread; coord: ``coord_adapter.send`` for the optional
    initial_message).

  The pure helper ``_validate_and_save_uploaded_files`` lifted from
  ``turnstone.server`` to ``turnstone.core.attachments`` as
  ``validate_and_save_uploaded_files`` so both processes can call
  the same kind-agnostic implementation.

  **§ Post-P3 reckoning item #1 done — coord gains create-time
  attachments.** Pre-lift ``coordinator_create`` accepted JSON only
  and ignored uploads; the lifted body parses ``multipart/form-data``
  on coord and saves attachments through the kind-agnostic storage
  layer. ``CoordinatorAdapter.send`` gained optional
  ``attachments`` + ``send_id`` kwargs so when a create request
  carries both ``initial_message`` and uploads, the attachments
  are reserved onto the dispatched first turn — the worker's
  ``ChatSession.send(..., send_id=...)`` consumes them on dequeue
  exactly the way interactive's create-with-attachments worker
  thread does. The ``send_id`` reservation token soft-locks the
  rows, and the adapter's failure path unreserves so a worker
  crash returns them to pending. The pure helper
  ``_reserve_and_resolve_attachments`` lifted from ``server.py``
  to ``turnstone.core.attachments`` as
  ``reserve_and_resolve_attachments`` so both kinds call one
  kind-agnostic implementation.

  Note on broadcast timing: coord's ``mgr.create`` fires
  ``emit_created`` (cluster collector fan-out) BEFORE the lifted
  body runs attachment validation. If validation fails on coord and
  the rollback (``mgr.close`` → ``emit_closed``) fires, the cluster
  events stream sees a phantom create→close pair. Cluster consumers
  handle this gracefully (same shape as any quick-create-close);
  decoupling ``emit_created`` from ``mgr.create`` would be a bigger
  refactor that doesn't belong in the verb lift. Interactive's
  broadcast (``gq.put_nowait("ws_created")``) is held until after
  attachment validation by the post-install callback, so interactive
  never sees the phantom pair.

  Five observable behaviour changes on the create response:

  - **Both kinds converge on 200 OK.** Pre-lift interactive
    returned 200 (default JSONResponse status); pre-lift coord
    returned 201. Picked 200 over 201 for response-shape parity
    with every other shared verb at the cost of REST-strict
    correctness — a one-time release note rather than ongoing
    client churn (the rest of the v1 SDK already uses
    ``response.ok`` per ``feedback_test_frontend_locally.md``).
    SDK consumers that branched on ``status == 201`` for coord
    must switch to ``response.ok``.
  - **Always-include response shape.** Pre-lift interactive
    returned ``{ws_id, name, resumed, message_count, attachment_ids}``
    (5 fields); pre-lift coord returned ``{ws_id, name}`` (2). The
    lifted body always returns the full shape, with ``resumed=False``
    / ``message_count=0`` / ``attachment_ids=[]`` on kinds whose
    post-install doesn't populate them. Coord callers will see the
    parity fields appear with default values.
  - **Both kinds converge on the manager-at-capacity 429
    semantic.** Pre-lift interactive translated ``mgr.create``'s
    ``RuntimeError`` to 400; coord already translated to 429. The
    documented contract on ``SessionManager.create`` is "raises
    RuntimeError when the manager is at capacity" — 429 (rate-
    limit / try-later) is the correct shape.
  - **Both kinds converge on the factory-misconfig 503 semantic.**
    Pre-lift interactive let ``ValueError`` propagate as 500 with
    a stack trace; coord already translated to 503 with the
    factory's remediation text. Operators get the actionable
    message instead of the trace.
  - **Both kinds get a correlation_id'd 500 on unexpected
    ``mgr.create`` failure.** Pre-lift interactive let unexpected
    exceptions propagate as 500 with a stack trace (potential
    information leak via frame names / file paths); coord already
    returned a correlation_id'd 500 with the message redacted. The
    lifted body adopts coord's safer pattern on both kinds.

  Two coord-specific parity gains:

  - **Coord rejects disabled skills.** Pre-lift
    ``coordinator_create`` silently allowed disabled skills to
    flow through to ``mgr.create`` — the row would create with a
    skill the operator had marked inert, surprising both the
    operator and the next user. The lifted body returns 400
    "Skill not found or disabled" matching interactive's
    behaviour.
  - **Coord audit-emit failures no longer 500.** Pre-lift
    ``coordinator_create`` already swallowed; pre-lift interactive
    let the failure propagate as 500. The lifted body wraps
    ``audit_emit`` in try/except + ``warning`` log, returning the
    successful 200 to the caller. Mirrors the close / cancel /
    open / events lift contracts.

  No legacy adapter is needed for create — both kinds already
  mounted ``POST {prefix}/new`` pre-lift; the lifted handler slots
  in at the same path on each kind.

  Three /review fixes folded into the same commit:

  - **Pre-lift's 400 on malformed ``notify_targets`` preserved.** The
    initial draft surfaced ``notify_targets`` validation errors from
    inside the interactive ``post_install`` callback, which the
    factory had no return-the-400 channel for — the only signal was
    to ``raise``, which the factory's generic exception handler
    turned into a redacted 500. Worse, by the time ``post_install``
    ran the workstream was fully built (audit row written,
    ``ws_created`` broadcast emitted), so a malformed-input request
    surfaced as "create failed" with the workstream actually live.
    Fixed by moving the ``notify_targets`` validation into
    :func:`_interactive_create_validate_request` (the pre-create
    gate), which returns the 400 before ``mgr.create`` runs and
    keeps storage clean. New regression test:
    ``test_create_lift_400s_on_malformed_notify_targets``.
  - **Skill-lookup storage failure now correlation_id'd.** The
    initial draft swallowed ``get_skill_by_name`` exceptions into
    ``skill_data = None`` and returned a 400 "Skill not found or
    disabled" — masking storage outages as user-input misses and
    making operator triage of skill-related reports impossible. The
    lifted body now lets the storage exception propagate to the
    same correlation_id'd 500 path that ``mgr.create`` failures
    use; the skill-lookup + version count + ``mgr.create`` all live
    inside one ``try / except`` so storage outages anywhere in the
    create-prelude get the redacted-message-with-correlation-id
    treatment instead of a stack-traced 500 leak.
  - **Whitespace-only ``skill`` field treated as empty.** The
    initial draft took ``body.get("skill") or ""`` literally — a
    payload with ``"skill": "  "`` would have hit
    ``get_skill_by_name(" ")`` and 400'd as "Skill not found".
    Pre-lift coord stripped via ``(body.get("skill") or "").strip()
    or None``; the lifted body now strips for both kinds (interactive
    never received whitespace-only skills from the web UI but the
    convergence is the safer default).
  - **Canonical skill name persisted to ``mgr.create``.** The initial
    draft's ``_interactive_create_build_kwargs`` /
    ``_coord_create_build_kwargs`` passed the raw ``body["skill"]``
    through, so a whitespace-padded request would have persisted
    ``"  my-skill "`` even though the lookup was done on the
    stripped name. The build_kwargs callbacks now thread
    ``skill_data["name"]`` (the resolved row's canonical name) so
    the persisted ``Workstream.skill`` matches the row that was
    actually applied — keeps later session-side ``skill`` lookups
    working regardless of how dirty the inbound payload was.

- **Coordinator scratchpad tool renamed: ``task_list`` → ``tasks``.**
  The tool name on the LLM-facing schema, the audit event name
  (``task_list.update`` → ``tasks.update``), the SSE
  ``tool_result`` event name (the coord-tree UI keys
  ``ev.name === "tasks"`` for /tasks-refetch debounce), and the
  log tag (``task_list.corrupt_envelope`` → ``tasks.corrupt_envelope``)
  all switch together. Operators with audit dashboards / SIEM filters
  / log greps that pinned the old prefix should update; the rename
  is observable on the wire, not just internal. Internal Python
  surface follows: ``CoordinatorClient.task_list_*`` → ``tasks_*``,
  ``ChatSession._prepare_task_list`` / ``_exec_task_list`` →
  ``_prepare_tasks`` / ``_exec_tasks``, ``_TASK_LIST_MAX`` →
  ``_TASKS_MAX``. The previous name compounded the bare word
  ``task`` (which collides with chat-template channels on local
  models — the same reason ``task_agent`` carries the suffix); the
  plural form sidesteps the collision and is more accurate, since
  the tool acts on the whole list rather than a single task.

### Security

- **Coord attachment endpoints are now kind-strict**
  ([Stage 2 P1.5]). The coord ``attachment_owner_resolver``
  resolves through the in-memory ``coord_mgr`` only — it does NOT
  fall back to storage. Without this, an
  ``admin.coordinator``-scoped caller could pass an *interactive*
  workstream ws_id to the new coord attachment endpoints; the
  generic ``get_workstream_owner`` storage call (kind-agnostic)
  would resolve cleanly and grant cross-kind read / write access
  to interactive attachments. The kind-strict resolver returns
  404 for any ws_id not currently held by the coord manager,
  closing the cross-kind path. Persisted-but-not-loaded
  coordinators must be ``open``ed before their attachment endpoints
  respond. Caught by /review pre-merge; no exploit observed.

- **Workstream state writes are now buffered through ``StateWriter``.**
  ``SessionManager.set_state`` no longer holds ``ws._lock`` across a
  synchronous Postgres ``UPDATE`` for non-terminal transitions;
  instead a ``StateWriter`` (constructed at app startup, started /
  shutdown by the lifespan) coalesces transient transitions per
  ws_id and flushes every ~1s. **Observable behavior change**:
  transient state (``thinking`` / ``running`` / ``idle`` /
  ``attention``) shows up in storage up to ~1s late; SSE consumers
  see it immediately via the adapter's ``emit_state``. Terminal
  ``ERROR`` transitions and ``close()`` write synchronously and
  remain durable on return. The bug-3 invariant — a closed row
  can't be resurrected by a buffered transient — is preserved by
  ``close()`` calling ``state_writer.discard(ws_id)`` (drops
  pending + waits for any in-flight flush) before its sync
  ``state='closed'`` write.

## [1.4.0]

User-visible additions: a full attachment system (images + text documents,
including pre-creation uploads), a unified dashboard composer, a Slack
channel adapter, per-call plan/task model selection with an admin UI, and
provider capability passthrough.

This release introduces two forward-only schema migrations
(`037_workstream_attachments`, `038_workstream_attachments_reserved_at`)
that the server applies automatically on first startup against an
existing 1.3.x database.  Both are additive; no data loss.  See
**Database migrations** below for details.

### Added

- **Workstream attachments** — images (png/jpeg/gif/webp, 4 MiB cap) and
  text documents (any `text/*` MIME, allowlisted application MIMEs, or
  known text extensions; 512 KiB cap; UTF-8 enforced).  Magic-byte image
  sniffing on upload; per-(ws, user) pending cap of 10.  Three-state
  lifecycle (`pending → reserved → consumed`) with reservation tokens
  threaded through `/v1/api/send` so queued multimodal turns can't lose
  files to overlapping sends.  Provider-side translation: Anthropic
  emits native document blocks; OpenAI Chat Completions inlines them as
  escaped `<document>` text blocks; Responses API emits `input_text`
  with the same wrapper. (#356)
- **Attachments at workstream-creation time** —
  `POST /v1/api/workstreams/new` accepts `multipart/form-data` (one
  `meta` JSON field plus 0..N `file` parts).  Files are validated and
  reserved onto the first turn before the dispatch worker fires; failure
  rolls back the fresh workstream so no orphan rows leak.  Web UI
  (new-workstream modal + dashboard composer), Python SDK, and
  TypeScript SDK all gained attachment support.  Cluster routing
  (`/v1/api/route/workstreams/{ws_id}/attachments`) extended to forward
  multipart bodies + preserve upstream headers (CSP, Content-Disposition).
  SDKs auto-generate `ws_id` client-side so cluster-routed callers can
  bind the body to the owning node before it lands. (#362)
- **Slack channel adapter** (Socket Mode) — mirrors the Discord adapter:
  per-user channel sessions via configurable slash command, DM routing
  without slash command, SSE event consumption, tool approval buttons
  with per-user owner enforcement, plan-review approve / request-changes
  modal, notification reply routing back into the workstream, and
  session recovery after restart via persisted recoverable route keys
  (the bot re-subscribes to existing Slack-routed workstreams when it
  comes back).  Install with `pip install 'turnstone[slack]'`. (#355)
- **Console admin UX support for Slack** — channel-link modal offers
  Slack alongside Discord; skill notify-on-complete forms expose a
  per-row channel-type dropdown (and no longer hardcode `discord`);
  per-platform `.scope-discord` / `.scope-slack` badge classes with
  theme-aware tokens (`--discord` / `--slack`) so light theme passes
  WCAG AA. (#365)
- **Per-call plan/task model selection** — `plan_model` and `task_model`
  are now distinct from the conversation model and from each other,
  with configurable reasoning effort per agent.  Three layers:
  - **Backend split** (`#54dd557`) — `ModelRegistry` gains `plan_model`,
    `task_model`, `plan_effort`, `task_effort`; per-kind overrides win
    over the legacy `agent_model`, which still works as the single-knob
    fallback.  `resolve_agent_alias(kind)` and `resolve_agent_effort(kind)`
    centralise resolution.  Loader validates effort against
    `{none, minimal, low, medium, high, xhigh, max}` with warn+drop on
    typos.
  - **Runtime configurability** (`#360`) — `ConfigStore` admin tab in
    the console UI lets operators switch alias and reasoning effort per
    agent **without restarting**.  `INHERIT_EMPTY_LABEL_KEYS` shows
    `(inherit)` for empty effort selections — distinct from the literal
    `none` choice which actually disables reasoning.  Routing overrides
    apply on `/v1/api/_internal/config-reload` (admin saves), and
    `model-reload` short-circuits when nothing changed so no in-flight
    clients churn.
  - **Per-call override** (`#361`) — the calling LLM can pass
    `model="<alias>"` to `plan_agent` or `task_agent` to override the
    operator-configured per-kind model for that one invocation.  Tool
    descriptions list the live registered aliases (refreshed when the
    operator hits "sync to nodes"), so the LLM always sees current
    options.  Bad aliases return a corrective error dict listing the
    available choices.  No whitelist — cost control is intentionally
    ceded to the model.  Plan-retry path reuses the alias so coaching
    reflects real model behaviour. (#360, #361)
- **Provider capability passthrough** — resolved per-model capabilities
  (vision, reasoning, native web search, thinking_mode, token_param,
  etc.) flow through to provider clients via a new `capabilities`
  parameter on `create_streaming` / `create_completion`, so feature
  gating no longer relies on string matching and admin-UI / config.toml
  overrides actually reach the provider.  Defensive shallow-copy in
  `_finalize_extra_body` so callers reusing the same dict across models
  are safe; deep-merge of `chat_template_kwargs` so operators can
  extend instead of silently overwriting. (#352)
- **Server compatibility layer for local model servers** — vLLM and
  llama.cpp profiles suggest the right thinking mode and per-server
  workarounds (`skip_special_tokens` for vLLM, `reasoning_format` for
  llama.cpp) during model detection.  Admin UI gains structured fields
  for server type, thinking mode, and extra body params, hidden for
  non-local providers (openai/anthropic/google).  New `thinking_param`
  text field surfaces the alias name (default `enable_thinking`;
  Granite/DeepSeek use `thinking`).  Verified end-to-end against real
  vLLM (Gemma 4 31B) and llama.cpp (Gemma 4 E4B) servers. (#352)
- **Claude Opus 4.7 support** — `claude-opus-4-7` capability entry
  (1M ctx, 128K output, adaptive thinking, `supports_temperature=False`,
  `thinking_display=summarized`).  New `ModelCapabilities.thinking_display`
  field — Opus 4.7 omits thinking by default but always sends summarized
  blocks back through the provider boundary.  Adds `xhigh` effort level
  to the global mapping and to Opus 4.7's `effort_levels`; admin-console
  skill-template dropdowns gained `xhigh` and `max` options.  Reasoning
  effort label capitalization aligned across all console dropdowns.
  (#357 — also in 1.3.1)
- **Dashboard composer refactor** — unified single-flow create from the
  per-node dashboard.  Multi-line textarea + collapsible Options panel
  (model / judge / skill) + paperclip + drag-drop / paste-image + chip
  strip.  Submit-button label dynamically toggles between `Create`
  (empty) and `Send` (text or attachments staged); Enter and click both
  go through the same `dashboardSubmit()`.  Replaces the inconsistent
  prior split where Enter created+sent raw and the button opened a
  separate modal.  Options panel state persists in `localStorage`;
  active non-default selections render as an inline summary chip beside
  the Options button; drag-over shows an explicit "Drop to attach"
  overlay.  The tab-bar `+` new-workstream modal also gained a paperclip
  + chip strip + first-message field so the same flow is reachable from
  both entry points. (#362, #366)
- **Workstream attachments — orphan reservation sweep** — periodic
  background sweep clears `reserved_for_msg_id` on rows whose
  `reserved_at` exceeds a 1-hour threshold, self-healing reservations
  leaked by process crashes between reserve and consume.  Backed by a
  partial index on `(reserved_at) WHERE reserved_at IS NOT NULL` so the
  scan stays cheap as the consumed-history grows.  Threshold tracks
  reservation age, not upload age, so a long-pending fresh send can't
  be racially unreserved. (#363)
- **`SendResponse` extended** — `attached_ids`,
  `dropped_attachment_ids`, `priority`, `msg_id` fields exposed in
  Pydantic + TypeScript SDKs so attachment-aware clients can detect
  partial reservations and dequeue queued messages. (#365)

### Changed

- **`plan_model` and `task_model` now split** from the conversation
  model and from each other — operators who rely on a single model for
  all three should set both `plan_model` and `task_model` explicitly in
  their config; otherwise both default to the conversation model so
  behaviour is unchanged. (#54dd557)
- **Channel notify-on-complete `channel_type` is no longer hardcoded
  in the admin UI** — operators creating notify targets through the
  skill admin form previously got `channel_type: "discord"` regardless
  of what they wanted.  Existing skill JSON values are unaffected; only
  newly created targets through the form differ. (#365)
- **Slack adapter approval previews** — capped at 600 chars per item
  with a 2700-char total budget so multi-tool approval batches never
  exceed Slack's 3000-char `section.text` limit.  Truncated batches
  show a `…and N more (preview truncated)` suffix. (#365)
- **PostgreSQL deployment image** swapped from `bitnami/pgbouncer` to
  `edoburu/pgbouncer` to track upstream releases and reduce image size.
  Environment variables remapped to the edoburu naming, ports updated
  to match documented expectations, and the Kubernetes Helm Chart link
  in the deployment docs now points at the same container.  Review
  your helm values if you depend on `bitnami`-specific environment
  variable conventions. (#353)

### Fixed

- **`plan_resolved` SSE broadcast** — when one client resolved a plan
  approval, other clients viewing the same workstream now have the
  approval card dismissed in sync. (#87a9af1)
- **Slack notification reply routing** — one notification reply
  previously pinned every later assistant response for that workstream
  to the notification thread until the bot restarted.  Reply-route
  override now clears on `StreamEndEvent`. (#365)
- **Slack plan-review mrkdwn fence** — plan content containing triple
  backticks (very common — plans often quote code) no longer breaks the
  surrounding fence and lets later content render as live markup.  The
  shared `_sanitize_slack_preview` helper splices a zero-width space
  inside any ``` ``` `` sequence while keeping single backticks
  readable. (#365)
- **Slack-routed workstreams now load the chat-specific system prompt**
  via `client_type="chat"`, matching Discord. (#365)
- **`/v1/api/workstreams/new` no longer emits a phantom
  `ws_created`/`ws_closed` SSE pair** when attachment validation
  rejects a multipart create.  Validation runs before the broadcast so
  failed creates are silent on dashboards. (#362)
- **Multipart Content-Type boundary preservation** in console routing
  proxy — `boundary=` parameter is case-sensitive and was being
  lowercased before forwarding to the upstream node, breaking parsing
  for clients that used mixed-case boundaries (most browsers). (#362)
- **Local-theme contrast for new badge colors** — `.scope-discord` and
  `.scope-slack` first shipped with raw hex that failed WCAG AA on
  light theme (1.8:1 / 2.4:1).  Theme-aware `--discord` / `--slack`
  tokens with proper light variants now pass. (#365)
- **Cross-user attachment fetch hardening** — `get_attachment_content`
  now scopes the row by `user_id` in addition to `ws_id`, so an
  unowned workstream can't be a vector for cross-user blob fetches via
  attachment-id guessing. (#356)
- **Attachment-list DoS guard** — `/v1/api/send` rejects
  `attachment_ids` lists longer than the per-(ws, user) pending cap
  with a 400, preventing hostile clients from blowing up the storage
  `IN (...)` clause. (#356)
- **Bounded LRU for upload locks** — the per-(ws, user) attachment
  upload-lock map now evicts the oldest unlocked entries past a soft
  cap, so the in-process map can't grow unbounded on long-running
  nodes. (#356)
- **3.12 CI deadlock on attachment uploads** — the upload-lock was
  initially an `asyncio.Lock`, but Starlette's `TestClient` runs each
  request on a fresh anyio task / event loop, so the cached lock's
  `_waiters` bound to the first loop and a later request would block
  on a Future from a closed loop (silent deadlock).  Switched to
  `threading.Lock` — loop-agnostic, and the critical section is one
  COUNT + one INSERT.  Same root cause is reproducible against any
  Starlette TestClient harness on Python ≥ 3.10; 3.12 surfaces it
  more often.  Production users on a single event loop weren't
  affected, but the test environment was. (#356)

### Security

- **Slack approval per-user authentication** — only the session owner
  can click Approve/Deny on a Slack tool-approval card.  Without this,
  any channel member with view access could approve dangerous tool
  calls initiated by someone else. (#355)
- **Attachment ownership masking** — cross-user/cross-workstream
  attachment ID lookups return 404 (not 403) so non-owners can't
  enumerate workstream existence by response code. (#356)
- Bumped Debian base image; remaining unfixable `jq` CVEs are
  documented and exception-listed. (#aaea4d3)

### Database migrations

- **`037_workstream_attachments`** — new `workstream_attachments` table
  with the lifecycle columns described above.  Indexes for ws_id,
  pending lookups, message linkage, and reservation scoping.
- **`038_workstream_attachments_reserved_at`** — adds `reserved_at`
  column for the orphan-sweep staleness signal, plus a partial index
  on `reserved_at IS NOT NULL` so the periodic scan is cheap.

Both migrations are additive and idempotent, and the server applies
them automatically on first startup against an existing 1.3.x database.
No manual `alembic upgrade` step is required — though running it
manually beforehand (e.g. as part of a phased deploy) remains safe.

### SDK

Python + TypeScript clients gained:

- `AttachmentUpload` type
- `upload_attachment(ws_id, filename, data, mime_type=None)`
- `list_attachments(ws_id)`
- `get_attachment_content(ws_id, attachment_id) → bytes / Blob`
- `delete_attachment(ws_id, attachment_id)`
- `send(message, ws_id, attachment_ids=...)` (extended)
- `create_workstream(..., attachments=[...])` — multipart variant with
  client-side `ws_id` generation for cluster-routed callers
- Console SDK: `route_create_workstream(attachments=...)`,
  `route_upload_attachment`, `route_list_attachments`,
  `route_get_attachment_content`, `route_delete_attachment`
- Refusal of `attachments + target_node` combination at the SDK
  boundary (the multipart routing layer doesn't honor `target_node`,
  so silently picking the wrong node is now an explicit error)
- `PlanResolvedEvent` SSE event with type guard, dispatched when one
  client (e.g. mobile) resolves a plan so other connected clients can
  dismiss their plan-approval modal in sync.  Available in both the
  Python and TypeScript SDKs. (#87a9af1)

### Operational

- **CI vendor-asset auto-download covers `hls.js`** — the
  `vendor-js.yml` workflow previously only iterated katex/hljs/mermaid,
  so Renovate bumps for `hls.js` failed the wheel-completeness check
  and required manual file downloads.  Detection loop now includes
  `hls`, so future Renovate bumps are merge-ready without intervention.
  (#354)

### Contributors

Thanks to the people who made this release happen — especially the
external contributors who picked up substantial pieces of work:

- **[@daoxley](https://github.com/daoxley)** — designed and shipped
  the Slack channel adapter (Socket Mode bot, per-user sessions,
  approvals, plan-review, notification routing).  Major new feature
  surface in #355.
- **[@pizzaandcheese](https://github.com/pizzaandcheese)** — replaced
  the deprecated bitnami pgbouncer image with the edoburu image,
  remapped environment variables, ports, and helm chart references.
  Operationally important for anyone running our reference Postgres
  deployment (#353).
- Renovate kept dependencies and the JS vendor tree current via
  several automated bumps.

If you're interested in contributing, channel-attachment ingest from
Discord + Slack is the headline 1.4.1 feature and a solid place to
start — see the open issues on GitHub or open one to scope a piece.

## [1.3.1]

### Added

- Backport: Claude Opus 4.7 support (provider capabilities, tokenizer,
  adaptive thinking). (#357)
