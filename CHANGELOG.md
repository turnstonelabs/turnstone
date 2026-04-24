# Changelog

All notable changes to turnstone are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) for
version numbers (`X.Y.Z`, with `X.Y.ZaN` / `bN` / `rcN` for pre-releases).

Three release tracks are maintained:

- **`stable/1.0`** — patch-only (`v1.0.x`)
- **`stable/1.3`** — patch-only (`v1.3.x`)
- **`stable/1.4`** — patch-only (`v1.4.x`)
- **`main`** — experimental (`v1.5.0aN`)

## [Unreleased]

### Changed

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
  both kinds share one implementation per verb. The `close`
  failure-status code standardized to 404 across both kinds (was
  500 on coord; "popped between .get() and .close()" is a not-
  found semantic, not a server error). Other shared verbs (`send`,
  `cancel`, `open`, `events`, `create`, `list`, `saved`, `history`,
  `detail`) keep their per-kind handlers — body convergence for
  those requires SessionManager-side refactors (e.g. Priority 1's
  worker-dispatch unification for `send`) or coordinated frontend
  changes (response-shape unification for `list` / `saved`) that
  fall outside Priority 0 scope.

- **TypeScript SDK bumped to 0.4.0** to flag the URL change for any
  1.5.0aN-era consumer of the experimental coord client. The
  `openapi-{server,console}.json` reference specs ship with the
  unified path tree.

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
