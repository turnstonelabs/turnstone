# Changelog

All notable changes to turnstone are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) for
version numbers (`X.Y.Z`, with `X.Y.ZaN` / `bN` / `rcN` for pre-releases).

Beginning with 1.8.0, `main` is the current stable line and `dev` is the next
release line. A `stable/X.Y` branch is created only when `main` advances beyond
that minor, so the current stable line never has two independently writable
branches. Earlier stable lines (`stable/1.7`, `stable/1.6`, `stable/1.5`) are
frozen.

## [Unreleased]

> **Before upgrading:** migration 077 renames `mcp_user_tokens` to `oauth_tokens` and its
> `server_name` column to `token_key`, preserving existing keys and encrypted data. Processes
> running older code cannot use this OAuth storage until their code is upgraded; OAuth operations
> on those processes can fail between migration and replacement. MCP API fields and token-keyring
> configuration names are unchanged. See [Shared OAuth storage](docs/oauth-storage.md).
>
> **Before upgrading:** migration 078 adds the workstream owner lease to `workstreams`. Upgrade
> every node, console and CLI that shares the database together: a process running older code
> ignores the lease, so while versions are mixed two processes can write the same workstream.
> Custom session factories must accept the new `workstream_lease` keyword argument and pass it
> to the `ChatSession` they build; creating or opening a workstream fails when they do not.
> `ChatSession.resume()` is removed: a session's workstream never changes after construction, so
> hosts reopen a workstream with `SessionManager.open`, which builds its session and loads it with
> `ChatSession.rehydrate()`. A host that embeds a `SessionManager` must call `start_lease_keeper()`
> once it is built and `release_leases()` at shutdown, or its leases lapse after 30 seconds; a
> custom `SessionEventEmitter` implements `on_lease_retired`, which announces a workstream another
> process took over.
> Custom storage backends must implement the lease methods (`acquire_workstream_lease`,
> `renew_workstream_leases`, `release_workstream_lease`), `end_foreign_node_watches` and
> `ensure_workstream_incarnation_snapshot` (which `open` now requires), and accept the `lease`
> keyword on every session-owned write; `bulk_close_stale_orphans` and
> `delete_stale_creating_reservations` no longer take `live_node_ids` (nor the latter
> `local_node_id`), because liveness is now the lease.
> `turnstone.core.policy.evaluate_tool_policy` and `evaluate_tool_policies_batch` are removed:
> `evaluate_loaded_tool_policies` returns `None` when the policies cannot be read, and a caller
> must then fail closed (see Security).
>
> **Before upgrading:** migration 079 replaces the `usage_events` `ws_id` index with a
> `(ws_id, timestamp)` index, built concurrently on PostgreSQL. Custom storage backends must
> implement `list_saved_workstreams`; `list_workstreams_with_history` takes only `limit` (no
> `kind`, `user_id`, `state` or `offset`) and returns only `(ws_id, alias, title, name, created,
> updated, message_count)`;
> `touch_workstream` is removed, and only conversation writes may change `updated`:
> `update_workstream_state`, `update_workstream_name`, `publish_deferred_create` and
> `bulk_close_stale_orphans` leave it unchanged, while `delete_messages_after` and
> `truncate_messages_tail` stamp it when they remove rows. `SessionEndpointConfig` drops
> `saved_state_filter` and `saved_loaded_lookup`, and `make_unified_saved_handler` its unused
> `permission_gate`.
>
> **Before upgrading:** migration 080 drops eight columns from skills (`prompt_templates`) and
> discards their values (see Removed): the model settings (`model`, `temperature`,
> `reasoning_effort`, `max_tokens`), the task-agent turn cap (`agent_max_turns`), the activation
> mode (`activation`, `is_default`) and the token budget (`token_budget`). It also deletes each
> workstream's saved `token_budget` key. This query lists the skills that set any of them: `SELECT
> name, model, temperature, reasoning_effort, max_tokens, agent_max_turns, activation, is_default,
> token_budget FROM prompt_templates WHERE model <> '' OR temperature IS NOT NULL OR
> reasoning_effort <> '' OR max_tokens IS NOT NULL OR agent_max_turns IS NOT NULL OR activation <>
> 'named' OR is_default = 1 OR token_budget > 0`.
>
> To keep a skill's model settings, put them on a model alias and select that alias where the skill
> is used. Task agents take their turn cap only from the `tools.agent_max_turns` setting, which is
> unlimited (-1) by default, so set it if a skill capped task-agent turns. Scheduled tasks, channels
> and coordinator spawns that relied on a skill's model run on the model they name, else the default
> alias. Workstreams created before the upgrade keep the model and settings they were saved with.
>
> A default skill's text no longer reaches the sessions that name no skill, existing workstreams
> included when they reopen. To keep it, move the text into a prompt policy, which every session
> reads live, existing ones too, or into a persona's prompt, which reaches only workstreams created
> afterwards (each keeps the persona prompt it was created with; a kind's default persona covers
> sessions that pick none). Unlike a default skill, both stay on when a workstream names a skill, so
> move only text that suits every such session, skill or not. Search-activated skills leave the
> system prompt; the model still finds them with the `skills` tool's `find` action. Nothing stops a
> workstream at a token count any more: a turn a budget prompt held now runs.
>
> The migration also fixes two kinds of saved data. A workstream switched to another skill before
> the upgrade kept the skill it was created with in its saved copy, which would come back on reopen;
> where the saved skill name is a different existing skill and the skill it was created with still
> exists, the copy is cleared, so the workstream reopens with the skill it switched to. The saved
> data cannot tell two rarer cases from that: a skill renamed before the upgrade whose old name a
> newer skill took is treated as a switch too, and a switch whose original skill was deleted, or
> whose target skill was later deleted or renamed, keeps the original's copy, shown under the name
> it switched to. (A skill cleared before the upgrade looks like an older reopen and still comes
> back.) A skill whose `notify_on_complete` fails the new check (see Changed) has it reset to `[]`;
> such a list never fired, because workstream create ignored it or could not deliver it. This query
> lists the skills with targets, to review them first: `SELECT name, notify_on_complete FROM
> prompt_templates WHERE notify_on_complete NOT IN ('', '[]', '{}')`. Nodes started with
> `turnstone-server --skill` must drop the flag (see Removed).
>
> Upgrade every node and console together: a process from before 080 fails every skill read against
> a migrated database. Custom storage backends: `create_prompt_template` no longer takes the eight
> fields (it required `is_default`), and `list_default_templates` and `list_skills_by_activation`
> are gone, as are the `turnstone.core.memory` helpers `list_default_skills` and
> `list_skills_by_activation`. Upgrade Python SDK clients too: older ones require `is_default` and
> fail to read skills.

### Added

- **Output-guard evals in `turnstone-eval` (#1291).** `--output-guard judge` scores the LLM stage on
  tool outputs with directives planted at known lines: detection, false positives, whether its flags
  stay inside the fixed vocabulary, citation precision and recall, and failed verdicts.
  `--output-guard locate` checks that a model finds the lines an advisory cites, alone and as the
  first of two results, and `--output-guard subagent` whether production's task agent acts on a
  planted directive with and without the advisory, counting only calls that carry it out (a command
  by the program it runs, a write by its path, a fetch by its URL), retrying a run that fails before
  it acts and keeping the error of one that fails after. Each subagent run keeps the agent's
  transcript, with the reasoning its provider returned, and `--output-guard grade` reads those
  transcripts back to a grader model: whether the agent noticed the planted instruction, what it did
  about it, whether it mentioned the guard's warning, and how it read the instruction, so a paid run
  is graded without being repeated. `--judge-model` runs the guard's judge on a model of its own,
  selected through `judge.output_guard_model`. `--provider` reaches the endpoint through the named
  adapter, so a model that takes mid-conversation system messages receives the advisory as one; a
  hosted provider reaches its own API and reads its own key variable. Cases ship in
  `turnstone/eval/scenarios/output_guard.py`. The mode flags `--nudges`, `--skill-adherence` and
  `--output-guard` are now mutually exclusive; given together, one ran and the other was silently
  dropped.
- **One live owner per workstream.** A process that loads a workstream holds a time-bounded owner
  lease on it, renewed every 10 seconds and judged against the database clock, and every write the
  session makes presents the lease's fencing epoch. Two servers sharing PostgreSQL can no longer
  both open the same workstream: the second answers 409 with code `workstream_lease_held` and the
  holder's node; the console follows a refused open to the holder and its delete proxy retries there
  once, while the node UI and the standalone shell say which node holds it. Concurrent opens of one
  workstream load it once and report `already_loaded` to the others. A paused or partitioned former
  holder cannot write history, state, configuration, attachments or lifecycle changes after another
  process took over; its copy stops generating, tells open panes why, and is unloaded. A non-holder
  cannot close or hard-delete a workstream another live process owns. SQLite keeps its
  single-process behavior: a new process takes the lease over when it opens a workstream, though
  until a crashed process's leases expire (up to 30 seconds), deleting, renaming or importing those
  workstreams without opening them is refused, and the refusal names the crashed process's node,
  which can be the restarted one. In the CLI, `/resume` and `/new` open the workstream
  in a tab of their own, carrying the current tab's approvals (and `/new` its model, persona and
  settings), and `/resume` of a workstream another tab has switches to that tab; one that fails
  says why and stays on the current tab (or, when that tab was a stopped copy of the workstream it
  named, brings the next one forward). The tab left closes once its messages are saved, retrying
  any that are not; one whose background programs still run stays open until `/ws close`. A
  workstream another process took over closes with a notice, the next one comes forward, and input
  typed for the closed one is dropped; `/resume` of such a tab opens it again, `/ws close` says
  whether a tab closed, moved elsewhere or is still saving, `/exit` works with none open, and a
  slash command that fails or is interrupted reports why and returns to the prompt instead of
  ending the CLI. A node that loses a workstream to another process announces `ws_unloaded` on its
  event stream (not `ws_closed`: it may live on elsewhere), its own dashboard drops the row,
  and the console's cluster stream forwards it with the node's ID, so dashboards list the
  workstream under its holder only.
  `turnstone_workstream_lease_events_total{event}` counts acquisitions, takeovers, conflicts,
  renewals and losses. See [Workstream Owner Lease](docs/architecture.md#workstream-owner-lease).

- **Attachment cases in `turnstone-eval`.** A case can attach text files (`attachments` entries
  with `filename`, `content` and an optional `mime_type`); each is classified like an upload and
  sent through the real user-turn path, so the model receives exactly what an uploaded file
  produces. `forbidden_actions` fails a run that makes any listed tool call, and
  `expected_content` lists regexes the final answer must match. `eval_attachments.json` uses both
  to measure whether a model answers from an attached or pasted file instead of looking for it on
  disk.
- **Anthropic workspace scoping per model definition.** An organization-level Anthropic API key
  in a multi-workspace organization needs the `anthropic-workspace-id` header on every request.
  The model editor gains a Workspace ID field for the `anthropic` and `anthropic-compatible`
  providers, stored as `capabilities.server_compat.anthropic_workspace_id`; the registry pins it
  on the alias's client so model turns, the Detect probe, the doctor check, and delegated-credential
  calls all carry it. Values must be visible ASCII of at most 128 characters; the console refuses
  anything else, or a value on a provider that does not speak the Anthropic protocol.
- **`turnstone-doctor` flags a Caddy running an outdated Caddyfile.** On a Docker install, the
  report compares the config Caddy loaded with the Caddyfile on disk and, when they differ, gives
  the `docker compose restart caddy` command that applies it.

### Changed

- **`/skill` quotes skill names in its operator note (#1292).** The note `/skill` writes into the
  conversation quotes each skill name as JSON, so a name cannot break the note into extra lines,
  and says `no skill` where it said `defaults`.
- **Skills check `notify_on_complete` like a workstream's `notify_targets` (#1292).** At most 10
  targets, each naming a `channel_type` and one of `channel_id` or `user_id`: a skill can no longer
  store targets that workstream create then dropped without a word or could not deliver. An explicit
  `null` leaves the targets unchanged. The `skills` tool's create, update and enable cards show the
  targets whole, and the approval judge sees them; the tool's `update` no longer sets `enabled`,
  which only `enable` and `disable` change, with their card. The shared check also refuses a target
  whose `channel_type` is `null`, so node workstream create (`POST /v1/api/workstreams/new` and the
  console routes that forward to it) and the console's schedule create and update now answer 400 for
  one. Such a target was stored without a type: a workstream's notification never arrived, and the
  node refused every run of a schedule that held one.
- **Saved sessions page, search and sort on the server (#1268).** `GET /v1/api/workstreams/saved`
  on nodes and the console takes `limit` (default 50, at most 200; `0` returns only the total),
  `offset`, `q` (a case-insensitive substring of at most 256 characters), `sort` and `order`, and
  its response adds `total`, `limit` and `offset`; a malformed parameter answers 400. Search folds
  letters beyond ASCII on SQLite, and on PostgreSQL as far as the database's locale does. Project
  names count for search and the project sort only for callers with `project.read`, who are the
  ones the dashboards can show them to.
  A saved session is now one with history that no process has loaded (no live owner lease), for
  both kinds: a node's list no longer includes workstreams loaded on it or on any other node, and
  the console's no longer includes interactive sessions that nodes have loaded. The dashboards'
  saved tables fetch each page, search and sort from the server, so they reach every stored
  session; sorting and searching pause while rows are selected for deletion, as paging already
  did. While a page loads its rows dim and keyboard focus stays on the control that asked for it,
  and a failed load on a node's dashboard shows the error with a Retry button, as the console's
  already did. Each dashboard keeps one request for the list in flight and asks once more after it,
  however many refreshes arrive meanwhile. The Python and TypeScript SDKs'
  `list_saved_workstreams` / `listSavedWorkstreams` take the same options and still read an older
  server's unpaged answer.
- **A workstream's `updated` is the last change to its conversation (#1268).** Saving or removing
  messages (rewind, retry) sets it; state changes, renames, opening, closing and cleanup no longer
  do. Lists ordered by `updated` (the saved sessions, the CLI's `/workstreams`, a project's
  workstreams, the console cluster view's stored coordinators and a coordinator's children) now
  order by real use, so a coordinator closed today but last used long ago no longer jumps into the
  cluster view's 200 most recent. `session.retention_days` ages an unnamed workstream from its last
  conversation change, so opening an old unnamed workstream no longer restarts its clock; naming it
  still keeps it. Cleanup of a workstream no process holds now waits for the idle timeout from its
  last conversation change rather than from its last state change, and orphan close and retention
  also leave a workstream whose owner lease ran out within their window, since a holder whose
  renewals are failing may still have it open.
- **Routing prefers a workstream's owner, and cleanup keys on leases.** The console router sends a
  workstream to the live node holding its owner lease (after any required node). Orphan close and
  stale-create recovery skip workstreams with a live lease instead of trusting node heartbeats, and
  boot prune, which had no liveness check, now skips them too, so a crashed process's workstreams
  are reclaimed whatever node id the next process carries, and orphaned coordinators are now reaped.
  On PostgreSQL, a workstream whose holder crashed can be reopened elsewhere after at most 30
  seconds, and the router stops sending requests to it once its lease expires. `turnstone-server
  --resume` waits once, up to that TTL, when its own previous process still holds the workstream,
  and it and the CLI's `--resume` report any failure to resume as a one-line error; resuming a
  workstream with no stored turns now opens it, with its saved settings, and resuming a
  coordinator from the CLI or server startup is refused. Coordinator deletes now run on the
  console. A watch is ended when another node opens its workstream, because a watch runs only on
  the node that created it, and the model is told why unless the workstream has no messages.
  Opening a workstream whose history or saved settings cannot be read answers 503 and can be
  retried, instead of opening it empty or on default settings, and opening one (or viewing it
  with write scope) on a node whose session slots are all busy answers 429, as a create does.
- **Watch delivery reopens the workstream like a pane.** A watch fire for a workstream that is not
  loaded now opens it through the session manager instead of building a separate session that
  switched to it, so a pane opened meanwhile joins the same session and no empty workstream is left
  behind. While nobody else is in it, a workstream opened this way approves its own tool calls,
  audited as `unattended_watch` rather than as skip-permissions, until the first client opens it,
  views it, sends to it, approves in it, stops it or attaches to its stream; a call an admin `ask`
  policy matches takes the normal approval flow instead, as in an attended session (the
  smart-approval judge, if enabled, or a person; the old restore approved it). A workstream with no
  stored turns ends the watch.
- **Containers run Turnstone under an init process.** The image's entrypoint is now `tini`, and
  the Helm chart's server and console pods set `shareProcessNamespace: true`, so the pod's
  pause container is PID 1. Either one reaps orphaned processes, such as the helpers stdio MCP
  servers leave behind, which Turnstone as PID 1 left as zombies. Turnstone now handles
  SIGTERM in containers as it already does elsewhere: it shuts down as before, but the
  container's exit code on `docker stop` or pod termination is 143 rather than 0. Overriding
  the image entrypoint (`docker run --entrypoint ...`) bypasses `tini`.

- **Shared model completion recovery.** Conversations, task agents, summaries, utilities,
  judges, and perception share a bounded retry allowance for empty responses and transient
  response failures. Task, summary, utility, judge, and perception calls also spend it on
  compatible gateways returning transient JSON errors with HTTP 200; the conversation loop treats those
  as ordinary creation failures with its existing retry, health, and fallback handling. Task
  and summary calls no longer multiply retries through additional caller loops. Accepted
  responses that may run server-side tools are never automatically replayed; compaction after
  a context overflow is not a replay and still runs. Direct eval and optimizer sampling keep
  their existing behavior.
- **Independent OAuth runtime.** MCP and model authentication share a process-owned runtime
  for token requests, credential rotation and durable writes. Model authentication works without
  an MCP manager and stays available when a persona disables MCP tools.
- **Shared delegated-auth modules.** OAuth/OIDC protocol code, generic encrypted token storage,
  and model-provider mint orchestration now have separate homes from MCP consent and transport.
- **Shared OAuth token storage.** MCP grants and model mint caches now use consumer-neutral
  storage names, and model lifecycle purges delete only their token rows. Shared operator
  guidance covers the deployment keyring, rotation, and credential lifecycle.
- **Python 3.13 minimum for Turnstone 1.9.** Python 3.11 and 3.12 are no longer supported; 1.8 is
  the last series that accepts them. Native installations must recreate their virtual environment
  with Python 3.13 or newer before upgrading. CI tests Python 3.13 and 3.14, and Docker images
  already include Python 3.14.
- **Anthropic SDK v1 and its HTTPX2 transport are now required (#1050).** The dependency is
  `anthropic>=1,<2`; the temporary `<1` cap is gone. Native Anthropic and anthropic-compatible
  streams normalize the SDK's HTTPX2 connection deaths through the same retry boundary as the
  OpenAI lanes, including a wire failure observed after a model-registry reload closed the client
  from another thread. SDK v1 removed the `temperature`, `top_p`, and `top_k` request parameters, so
  capability-gated temperature now reaches the wire through the request's `extra_body` with
  unchanged send/omit semantics, and an operator `server_compat.extra_body` pin of the same key
  keeps precedence over the session value. Anthropic connections follow HTTPX2's operating-system
  trust store by default, as OpenAI connections already do; deployments that relied on a modified
  `certifi` bundle must install that CA in the system store or set `SSL_CERT_FILE` /
  `SSL_CERT_DIR`.
- **Credential headers are refused in `extra_headers`.** Both SDKs merge caller headers over
  their own credential header case-insensitively, even over a `with_options(api_key=...)`
  delegated credential, so an injected `x-api-key` or `Authorization` header would silently
  replace it. Every provider adapter now raises at request assembly instead; delegated
  credentials ride `with_options(api_key=...)` only.
- **URL fetches use HTTPX2.** `web_fetch` and URL targets of `open_preview` now run on HTTPX2 and
  its connection layer, with the same per-hop address screening, pinned connections, manual
  redirects, and decoded-size ceiling. Compressed bodies are decoded in bounded pieces, so a small
  compressed response can no longer expand in memory before the ceiling applies, and fetches now
  accept brotli- and zstd-compressed responses as well as gzip and deflate (HTTPX2's `brotli` and
  `zstd` extras, which add `brotli` and, before Python 3.14, `backports.zstd`). Certificates are
  verified against the operating system's trust store instead of the `certifi` bundle; deployments
  that trusted a private CA through a modified `certifi` bundle must install it in the system store
  or set `SSL_CERT_FILE` / `SSL_CERT_DIR`, which apply to the whole process. HTTPX2 and its
  connection layer log at `WARNING` and above, as HTTPX did, so fetched URLs stay out of `INFO`
  logs; if something lowers that level (for example `ANTHROPIC_LOG=info`), HTTPX2 request lines show
  only each URL's scheme, host, and port, since a fetched URL can carry a secret anywhere after its
  host, and the rest of each line passes through the output guard's credential redaction. This also
  removes the OpenAI SDK's per-request `HTTP Request:` lines logged at `INFO` since 1.8.0; model
  calls are still logged as `model_turn.completed`. The `httpx2` minimum is now 2.12, and
  `httpcore2` replaces `httpcore` as a direct dependency.
- **URL fetches ask for web pages.** `web_fetch` and URL targets of `open_preview` send
  `Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8` and
  `Accept-Language: en-US,en;q=0.9` on the first request and every redirect. Hops to https URLs and
  loopback hosts also send navigation metadata (`Sec-Fetch-Dest: document`,
  `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Site: none`), which browsers omit on plain http. The
  `User-Agent` is still `turnstone/1.0`. Other content types remain acceptable, but an endpoint that
  negotiates content may now return HTML where it previously returned JSON or CSV, and some image
  hosts (for example `i.imgur.com`, `i.redd.it`) redirect a direct image link to their HTML page,
  which a preview shows without its scripts.
- **The output guard judges on `judge.model` when it has no model of its own (#1291).** With
  `judge.output_guard_model` empty, the guard's LLM stage ran on the session's model, the model
  whose tool output it judges. It now uses `judge.model`, and the session's model only when that is
  empty too; a set alias that is not registered is passed over with a warning. Installs that set
  `judge.model` but not `judge.output_guard_model` switch the guard's model on upgrade: the guard
  then shares that alias's `max_concurrency` with the intent judge, and long tool output may exceed
  a small judge's context window, which skips the LLM stage for that result. Set
  `judge.output_guard_model` to keep a separate model for the guard. A workstream's own judge model
  (the launcher's judge picker, the create API's `judge_model`, the CLI's `--judge-model`) is that
  workstream's `judge.model`, so it runs the guard too unless `judge.output_guard_model` is set.

### Removed

- **Skills no longer choose the model, its settings or the task-agent turn cap (#1292)**
  *(BREAKING)* — a skill's model alias, temperature, reasoning effort, max tokens and agent max
  turns are gone from the skill editor, the admin skill API (`SkillInfo`, the create and update
  bodies, the SKILL.md parse preview), the `skills` tool and the Python and TypeScript SDK types. A
  workstream runs on the model its create request names, else the default alias, and that alias
  supplies temperature, reasoning effort and max tokens; task agents take their turn cap from the
  `tools.agent_max_turns` setting. Installing a SKILL.md now ignores its `model:` and `effort:`
  keys, like any other unsupported key, reversing the 1.6.0 ingestion. Rows of the console's
  `GET /v1/api/models` no longer carry `effort_ladder`, which only the skill editor read; the admin
  model form keeps its ladder through `POST /v1/api/admin/models/effort-ladder`.
- **Skills lose their activation mode and token budget (#1292)** *(BREAKING)* — default skills
  (`activation` `default`, mirrored in `is_default`), whose text joined every session that named no
  skill, are gone, and so is the `<available-skills>` list of search-activated skills in every
  system prompt: a skill applies only when a workstream names it, and the model finds skills with
  the `skills` tool's `find` action. The skill `token_budget`, which asked for approval once a
  single model call passed it, is gone with its `__budget_override__` prompt on nodes and in the
  CLI. The skill editor's Activation and Default controls and Token Budget field, the admin skill
  API (`SkillInfo` and the create and update bodies), `GET /v1/api/skills` rows, the `skills` tool's
  arguments and the Python and TypeScript SDK types lose `activation`, `is_default` and
  `token_budget`; a request that names them has them ignored. `turnstone.core.skill_search`, which
  nothing used, is removed.
- **`turnstone-server --skill` is removed (#1292)** *(BREAKING)* — it gave its skill to every
  workstream whose create request named none: a node-wide default skill, without the saved copy a
  named skill gets. A workstream now runs a skill only when its create request, scheduled task,
  channel or `/skill` names one. The CLI keeps its `--skill`: the skill for each workstream it
  starts from scratch (`/new` copies the current tab's skill, and a resumed workstream keeps its
  own).

### Fixed

- **A reopened workstream keeps its skill (#1292).** Reopening, forking or copying (`/new`) a
  workstream created with a skill dropped the skill's name and put its saved text, unrendered, in
  the cached system prompt that task agents inherit: `$ARGUMENTS` and `${TURNSTONE_*}` stayed
  literal, `/skill` reported no skill, and the next save erased the name. A skill switched or
  cleared after creation also came back on reopen. The skill now keeps its name and renders like any
  named skill, with the text it had at creation even after the skill is edited, renamed, disabled
  or deleted: disabling or deleting a skill only keeps it out of new workstreams. That covers
  workstreams created through a node's create API (the console's interactive launcher, the API,
  schedules, channels and coordinator spawns); coordinators created from the console launcher, CLI
  `--skill` sessions and skills loaded mid-conversation are looked up by name on reopen (#1312,
  #1320). A switch or clear survives a reopen. `skills(load)` of a skill that was missing when the
  session loaded it now applies the skill once it exists, where it answered that the skill was
  already active.
- **`skills(find)` ranks past the first page (#1292).** A query ranked only the first page of skills
  in storage order (100 by default), so a better match past it was never found, and it returned at
  most 50 rows whatever its `limit`. It now ranks up to 500 filtered skills and returns up to
  `limit` of them (50 by default with a query).
- **MCP shutdown stops the work callers wait on.** Shutting down the MCP client left work that a
  caller was waiting on running, so the caller waited out its own timeout (two minutes for a tool
  call, forever for adding a server), and garbage collection later closed the abandoned work on
  whatever thread it ran, reporting "Exception ignored" errors. Shutdown now cancels that work and
  closes every MCP server connection under one 20-second deadline, which keeps MCP shutdown within
  a 30-second stop budget, and each waiting caller gets "MCP client is shutting down" at once. A
  server that never answers the request ending its session gets 3 seconds, so it can no longer
  hold a connection open through shutdown. A tool call stopped mid-flight is recorded with an
  unknown outcome, because it may already have reached its server; a shutdown never counts
  against a server's circuit breaker.
- **An MCP server removed while a reconnect waited stays removed.** An operator reconnect, or the
  startup pass that connects every configured server, could wait behind a server's removal and
  then connect it again from the config it had read, so the removed server's tools stayed in use
  until the next restart. Both now skip a server removed while they waited. A reload that added or
  removed a server while the startup pass was still running also stopped the pass, which skipped
  the remaining servers and never started the health checks that reconnect a dropped server; the
  pass now reads each server's config when it reaches that server.
- **Saved sessions list all stored history (#1268).** The saved list returned at most the 50 newest
  sessions of each kind that the caller could see, without a total, so the dashboard's pager and
  search never reached older sessions. It now pages through every session the caller may see, with
  an exact total. A coordinator left behind by a console that crashed or restarted appears once its
  owner lease expires (within 30 seconds), instead of only after the idle timeout closed it, or
  never when the idle timeout is 0. Closing a session, by hand, at its idle timeout or in cleanup,
  no longer stamps it as just updated, so it keeps its place in the list instead of jumping to the
  top (see the change to `updated`); sessions closed before this release keep the time stamped
  then. On PostgreSQL the list's latest-usage lookup could scan every newer usage event for each
  session (over a second for a few hundred sessions); migration 079's index makes it a short index
  scan.
- **Saved lists keep their NAME column, and empty states are legible (#1268).** The dashboards'
  saved tables dropped columns by the window's width, so beside the side rail or in a split pane
  NAME could shrink to nothing (at a 1024-pixel window on the console); they now go by their own
  width and drop one lower-value column at a time until NAME has 200 pixels: ID first (whenever
  NAME would have less than 280), then the message or children count, PERSONA, PROJECT, MODEL and
  the console's KIND, keeping the sorted column for last; if even that one goes, the footer names
  the order. A long name and its skill chip share the cell, the chip names its skill on hover and
  the row's accessible name includes it, and cut-off text keeps a gap before the next column. The
  header's search box and Delete move to their own row when they no longer fit beside the title,
  the search box narrowing as far as it must so Delete is never pushed out of view; in a narrow
  pane the pager drops below the count instead of squeezing it; Retry says so while it works; and
  the CTX column no longer goes blank on phones. Empty and error messages in both apps lost an
  extra opacity that put them below WCAG AA contrast.
- **The output-guard judge gets its model's output budget (#1291).** The LLM stage capped every
  verdict at 512 tokens, and the cap counts reasoning as well as the answer, so a thinking model
  could spend it before writing the verdict; the verdict was then dropped and only the regex stage
  stood. The cap is now the guard model's own: the alias's `max_tokens`, else the `model.max_tokens`
  setting (32,768 by default), never above the model's advertised maximum output, and fitted to the
  context window the prompt leaves. A change to `model.max_tokens` reaches the judge without a
  restart. To size its prompt against the window, the judge now counts each ASCII digit as a token,
  text outside ASCII by its UTF-8 length and a dense base64-like run (mixed case, with a digit) at
  0.6 tokens a character, then allows for up to 1.4 times that count: numeric output such as JSON,
  CSV or logs ran up to 3.5 times the old character estimate, Chinese, Japanese, Korean and base64
  text up to 2.4 times, and a server such as vLLM refuses a request whose prompt and cap together
  exceed the window. With an effort resolved for the guard, a model that takes a fixed thinking
  budget, as some of Anthropic's do, now thinks on every judged result, at temperature 1.0; the 512
  cap left it no room to. A judged result can now use up to the cap and take up to
  `judge.output_guard_llm_timeout`; a smaller `max_tokens` or a lower effort on the guard's alias
  bounds both.
- **A failed request no longer strands a web search (Anthropic).** When the model called web
  search alongside another tool, the API held the search back until the next request. If that
  request failed (an exhausted credit balance, say) and a new message followed, every later request
  was rejected (`web_search tool use with id ... was found without a corresponding
  web_search_tool_result block`) and the workstream could not continue. Such a search is now sent
  back with the API's own `unavailable` error, so the model sees that it failed and can search
  again in a later turn. A mid-turn compaction that kept a reply opening with a search result, but
  summarized away the search that produced it, no longer breaks the workstream either.
- **Native tool search keeps working after its first round (Anthropic).** The search's result was
  left out when the conversation was sent back, so the API rejected every request from the second
  one after a search onward and the workstream could not continue. The result is now passed back
  unchanged, and workstreams already stuck this way continue. A tool that a search found but a
  later request no longer offers, for example because its MCP server is offline, is offered as no
  longer available, since the API rejects a request whose history names a tool it does not have.
- **A saved workstream with no messages keeps its settings.** Opening one (from a pane, and now
  also `--resume` and `/resume`) applied the constructor's defaults instead of its saved model,
  sampling, instructions and skill, and the next settings change wrote those defaults over the
  saved ones. It now opens with its saved settings.
- **The model keeps its own searches in its history (OpenAI).** Replayed history left out the web
  searches and tool searches the model ran itself, so it searched again for tools it had already
  found. Both now replay where they ran, and a call to a tool that a search loaded carries the
  namespace the API requires for it. A replayed web search shows the model its query or the page
  it opened, not the results, which the API does not take back. Compatible endpoints and xAI
  replay as before. A workstream that searched misses the prompt cache once, on its first request
  after the update. One that searched again for tools it had already found resends each of those
  searches, with the definitions it loaded, until it compacts.
- **A refused tool call says the tool is not available, not "Unknown tool" (#1287).** Tools can
  leave a session mid-conversation: an MCP server drops one or goes away, or another user sends on
  a shared workstream, whose tools follow the sender. When the model called one, it was told
  "Unknown tool" and to use a listed name exactly, and the operator saw "Model called unknown
  tool", as if the model had made the name up. Both now say the tool is not available now, and the
  model is told it may have been removed or be misspelled, or, on a shared workstream, belong to
  another user. The tools that reply lists are the ones the request offers, where it used to name
  every built-in tool, including coordinator tools in a regular session, tools a persona hides and
  tools an operator revoked. With tool search on, it lists the tools offered outright and says more
  may be found by searching, and a task agent sees its own tools.
- **A storage error while opening a workstream no longer replaces its saved settings.** Building the
  session saves its defaults when no settings are saved, and a read that failed counted as none
  saved, so one failed read could overwrite the saved model, sampling, instructions and skill. That
  open, or a create that hits the same failure, now answers 503 and can be retried.
- **Warning toasts look like warnings.** Ten warning toasts in the shell, the node UI and judge
  verdicts asked for a style the toast component does not have and fell back to the neutral one;
  they now use the warning style.
- **Hosted search for Astra (#1191).** The capability table now enables native web search
  when the session offers `web_search`, without requiring a model capability override.
- **Chat search usage no longer inflates replay context (#1191).** Requests with hosted search use
  the local context estimate instead of anchoring on billed input that includes search results,
  including automatic search without explicit search options. Text calibration remains unchanged.
  Automatic search also marks the request as using native tools, preventing an automatic replay of
  an accepted response.

- **Stream cleanup after callback failures.** Chat and Responses adapters close the underlying
  SDK response when a local chunk callback interrupts generation, preserving the original error.

- **MCP catalogs are read past their first page (#1225).** A server that paginates its tools,
  prompts, resources or resource templates had only its first page published, on connect and on
  refresh, for shared and per-user servers alike. Each list is now followed to its last page,
  within the existing per-server cap of 1,000 entries, which still truncates with a warning. An
  entry listed twice is published once. Empty pages that still point onward are read past. The
  walk also stops, with a warning, when the server repeats a cursor, sends ten pages in a row
  with nothing new, or answers a later page with a JSON-RPC error; it keeps the pages already
  read, so a catalog is never published shorter than its first page.

- **MCP HTTP failures release waiting calls promptly (#1223).** Shared and per-user connections
  capture upstream HTTP status before the SDK loses it. A 5xx counts against the circuit breaker;
  a 404 on a held session causes reconnection on the next call. HTTP 401/403 failures remain
  breaker-neutral, including shared connections, and per-user auth refresh and consent handling
  are preserved. Tools, resources, and prompts also stop waiting when their transport owner exits.
  Caller deadlines and protocol errors returned with HTTP 200 remain breaker-neutral.

- **A malformed MCP tool no longer sets off a reconnect loop (#1224).** The MCP SDK rejects a
  server's whole tool list when one entry fails its validation, such as a tool with no
  `inputSchema`, so that server publishes no tools; keeping the valid ones would need SDK
  internals. The server's status now names the entry and the field (`InvalidCatalogError: MCP
  server 'docs' lists an invalid tool 'search' at tools[1].inputSchema: Field required`), and so
  does each user's status for a per-user server. The failure no longer counts against the circuit
  breaker. A shared server stops offering the tools, resources and prompts from its last good
  connect (its prompts' templates are removed until it recovers), and is retried every five
  minutes instead of on the reconnect backoff of up to a minute; an operator reconnect still
  retries at once. A refresh that meets a malformed entry keeps the last good catalog and is
  retried like any failed refresh. A malformed entry on a later page fails the same way instead
  of cutting the catalog short, and a tool result that does not match the protocol's schema no
  longer counts against the breaker either. A shared server's status also shows why a reconnect
  failed, where before it showed only a failure at startup.

- **Processes started by a stdio MCP server no longer outlive it (#1226).** A server that
  started a helper process and then exited cleanly when its stdin closed left the helper
  running, so every reconnect, reload, removal or shutdown leaked another. Once a stdio
  server's connection has closed, whatever remains of its process group gets SIGTERM, then
  SIGKILL after one second; the server itself keeps its usual window to shut down. Containers
  now reap the stopped helpers; see the container init change above.

- **Shutdown no longer deletes MCP prompt templates (#1146).** A catalog refresh still waiting
  on a reconnecting server when Turnstone shut down could finish during cleanup, after shutdown
  had emptied the prompt catalog, and delete every MCP-sourced prompt template; the next sync
  recreated them as new rows. A refresh abandoned at shutdown no longer syncs when cleanup
  finishes it, and no prompt-template sync runs once shutdown starts emptying the catalog.

- **Watch output is delivered as tool data (#1235).** Watch notices carry metadata and a
  `watch(action="read", name="<watch id>")` hint. Command output and condition exception details
  are returned through the normal tool-result path and its configured output guard, rather
  than entering model context with operator authority. The watch-result card still shows the
  output, and completed watches remain readable by name or ID. Delivered snapshots remain
  readable in forks and after compaction or checkpointed resume, even if the terminal poll
  update failed or the source was compacted before forking.

- **Web UI typography.** Locally served font subsets retain the requested stylistic
  features, real italics and the 800 weight used by attention chips. Bundled symbol
  fallback and information icons give interface marks consistent shapes across platforms (#1231).

- **GPT-6 effort changes preserve the cached prompt prefix (#1246).** Supported standard
  Responses requests keep request-level effort at its initial value and replay later changes
  as `configuration_update` input items. Accepted turns record their resolved effort, so
  resume and fork preserve update positions; compaction starts a new baseline. Pro mode,
  requests with sampling parameters, compatible endpoints, and a return to an unknown server
  default retain request-level effort.
- **`THIRD-PARTY-NOTICES` lists the versions the package ships.** Its KaTeX, highlight.js, Mermaid
  and hls.js sections named older versions than the vendored files, because the update script
  rewrote version references only in source files and every bump left the notices behind. The
  headers now read KaTeX 0.18.10, highlight.js 11.12.0, Mermaid 12.0.0 and hls.js 1.7.3, and each
  section reproduces the license file shipped with the library, which restores the Brightcove
  copyright notice that the hls.js license carries for code derived from videojs-contrib-hls. The
  update script now rewrites the notices header on each bump, and tests fail when any
  version-named directory under `shared_static/`, fonts included, lacks a section whose header
  names its version and which reproduces or names its `LICENSE` file.
- **GPT-6.1 Sol requests built from generic defaults.** `gpt-6.1-sol` had no capability row, and its
  dotted version matched none of the GPT-6 rows, so it fell through to the default OpenAI row: a
  configured reasoning effort was dropped, a configured temperature was sent although the model
  always reasons and its guide says to remove sampling parameters, a model definition left on
  auto-detect ran with the 32,768-token fallback context window instead of 1.05M, output was capped
  at 64K instead of 128K, and the model was treated as unable to see images. It now has a row with
  the documented `low` through `max` effort ladder (no `none`) and default `medium`, no temperature
  at any effort, vision, PDF input, and tool search, and the 30-minute `prompt_cache_options`
  lifetime now covers GPT-6 point releases as well. A GPT-6 model definition whose window was saved
  as 32,768 from the Models tab's Detect result keeps that value until it is set to 0 (auto-detect)
  or 1,050,000.
- **Native mid-conversation system messages rejected after a wake or approval feedback.** On Claude
  models that take operator notes as native mid-conversation system messages (Opus 4.8, Opus 5 and
  5.5, Fable 5 and 5.1), an idle wake, such as a finished watch, background shell or child, sent its
  note right after the assistant's turn, and feedback typed with an approval after an advisory put a
  user message right after the note. The API rejects both with a 400, and because the note stays in
  the history, every later request in the workstream failed the same way. The Anthropic request now
  places such a note after the user turn that follows it, or behind a short placeholder user turn
  when it would otherwise follow the assistant's turn.
- **Claude Sonnet 5.5 requests failing after a history edit.** `claude-sonnet-5-5` had no capability
  row and resolved to the Sonnet 5 row, so its requests never opted into the degrade mode for
  thinking bound to the conversation prefix. With reasoning replay enabled for the model, on
  Anthropic organizations created on or after 2026-08-31, a replayed thinking block whose earlier
  conversation had since changed, for example after compaction, failed the whole request with a 400,
  and retries failed the same way. The model now has its own row and sends the binding control, so
  such blocks are dropped server-side, unbilled, and the request proceeds. Unlike Sonnet 5, and like
  Opus 5.5 and Fable 5.1, it also receives operator notes, such as nudges and messages the user
  queues mid-turn, as native mid-conversation system messages.
- **Small models looking for an attached file on disk.** A text attachment reached the model as
  a bare document block after the user's message, with nothing saying it was the file the user
  named or that it was not in the workspace. Small local models answered "summarize notes.md" by
  calling `read_file` on the name, searching the workspace, and reporting that the file does not
  exist, and in chat offered to create it. Each attached text file (a long paste included) is now
  introduced by a label naming it and saying attachments are not saved to disk, and `read_file`
  on a missing path named like an attachment says the user attached that file and points at their
  message instead of the filesystem.
- **GPT-6 Sol and Luna requests built from generic defaults.** `gpt-6-sol` and `gpt-6-luna` had no
  capability rows and fell through to the default OpenAI row: a configured reasoning effort was
  dropped, a configured temperature was sent while reasoning ran at the server default, which the
  models' guide says to remove, a model definition left on auto-detect ran with the 32,768-token
  fallback context window instead of 1.05M, output was capped at 64K instead of 128K, and the models
  were treated as unable to see images. Both models now have rows with the documented `none` through
  `max` effort ladder, temperature only at effort `none`, vision, PDF input, tool search, and the
  30-minute `prompt_cache_options` lifetime.
- **Claude Opus 5.5 requests failing after a history edit.** `claude-opus-5-5` had no capability row
  and resolved to the Opus 5 row, so its requests never opted into the degrade mode for thinking
  bound to the conversation prefix. On Anthropic organizations created on or after 2026-08-31, a
  replayed thinking block whose earlier conversation had since changed, for example after
  compaction, failed the whole request with a 400, and retries failed the same way. The model now
  has its own row and sends the binding control, so such blocks are dropped server-side, unbilled,
  and the request proceeds.
- **Context gauge after a server-side tool loop.** When the model ran several web searches inside
  one response, the provider's closing usage reported billing totals summed across its sampling
  passes, and the session took them as its context size. On the Anthropic lane a four-search turn
  reported three times the real figure: the status bar showed it, the token estimate was calibrated
  on it, a tool result was dropped for an exhausted budget, and a two-message conversation was
  compacted. On the OpenAI Responses lane hosted search reported twice the real figure. Adapters now
  report what the request carried and what the server appended, and flag cumulative counters; one
  shared rule turns that into the context anchor and the calibration denominator for the session,
  task agents, and the agent context badge. Reported cache reads still feed the usage records.
- **Context estimate on the turns after a server-side search.** The search results the provider
  appends to a response are replayed to it on every later request as opaque blocks the character
  measure cannot see, so from the next turn on the estimate was calibrated about 2.3 times low: new
  text and tool results were over-estimated by that factor, tool results were cut at less than half
  the intended ceiling, and the shrinkage lasted until compaction removed the results. The assistant
  turn now records the provider's own count of what it appended, the accepted turn is charged its
  completion count plus that count, the estimate charges it like the fixed image charge, only toward
  the provider that replays the lane, and a request that carries such a charge is not a calibration
  sample, so the ratio keeps its value, the default if nothing had calibrated it yet, until
  compaction removes the lane or another provider family serves a request; a model switch to another
  provider, which rebuilds the turn from its text, charges nothing. The count is persisted with the
  turn and survives reopen and fork; it is refused above the lane's own window, and a lane whose
  window nothing names records nothing, so a broken usage report cannot poison a workstream. A turn
  under reasoning replay off is over-charged by the thinking the provider fed back into its own next
  pass, bounded by its completion count. A compaction whose estimate did not shrink is logged with
  both figures and the ratio, beside the marker the card already shows.
- **Task-agent reply with no output count.** A task agent's reply whose provider reported no output
  token count is now charged from its text, the rule the session's own accepted turn already
  applied, instead of a single token, so the agent's context estimate no longer runs low after such
  a reply.
- **Incomplete task results and perception fallback.** Failed task completions now report an
  error, with any earlier partial work explicitly labelled incomplete. Optional perception
  memoizes a model that produces no description for the same binding generation and gives
  backend or local failures a 60-second cooldown, so neither repeats attachment processing on
  each send while a failed backend can still recover afterward.
- **Context-window stop reason.** A response that ends with Anthropic's
  `model_context_window_exceeded` stop reason was persisted as a clean completion, with no warning
  and no compaction. It now surfaces as the same context overflow an over-long request already
  produces, so the conversation is compacted and the turn retried for a complete answer.
- **Console forms on narrow panes.** The rule that collapses two-column form rows to one column
  on a narrow pane never applied: it was declared above the base rule it needed to override, and a
  container query adds no specificity. It now sits after it, so a tight split or a phone-width
  pane gets one column with an even rhythm between fields, and the model editor's provenance line
  wraps as a sentence instead of shredding the model id. Capability tiles stay two-up.
- **Label hints in the dark theme.** The small hint next to a form label was dimmed below the
  4.5:1 contrast floor; it now renders at full opacity in both themes, and the explanatory notes
  that stand on their own keep a subordinate size and colour instead of inheriting body text.
- **MCP OAuth consent return URLs.** Malformed URLs no longer abort consent; the callback
  falls back to the home page when the return URL contains invalid ports or brackets.
- **MCP submissions during shutdown.** Pool priming, catalog eviction and static server
  reconnection tolerate the loop closing between admission and scheduling. Rejected coroutines
  are closed, and reconnection reports its normal failure instead of an internal attribute error.
- **Per-user MCP OAuth refresh.** Discovery and refresh requests now use their owner's HTTP
  client, preventing cross-loop failures after browser sign-in.
- **OAuth cancellation and shutdown.** Cancelled requests settle started token writes before
  releasing their runtime locks. Bounded shutdown tracks advisory-lock workers, and unavailable
  runtimes return transient/no-token results without revoking grants or changing backoff.
- **Judge spend in the usage records.** The intent judge and the output-guard judge ran their
  model calls outside the auxiliary accounting that titles, compaction summaries, web-fetch
  extraction, and task agents use, so their tokens reached neither the usage records nor the
  node token metrics. Both judges now record every completed model call under the judge's own
  model, which is the configured judge alias's model when one is set. Like the other auxiliary
  calls, judge calls do not move the live context gauge of the workstream they run for.
- **The dashboard keeps one HTTPS certificate across restarts.** A browser sends no server name
  when it dials an IP address, so Caddy named the certificate after its container's Docker
  address, which changes whenever compose recreates the container, and issued a new one each
  time; every certificate also expired after 12 hours. Such connections now get one certificate
  for `127.0.0.1`, and each certificate Caddy issues for the dashboard and the SearxNG UI is valid
  for a year and kept in the `caddy-data` volume, so the certificate a browser accepted stays the
  same. Caddy reads its Caddyfile only when it starts: re-running `run.sh` now restarts it, and
  otherwise run `docker compose restart caddy`. Certificates already issued are replaced as they
  come due. Caddy also closes open dashboard streams within 5 seconds when it stops; before, a
  restart with the dashboard open waited out Docker's 10-second stop timeout and was killed.

### Security

- **The model reads only framework-written output-guard findings (#1291).** With the LLM stage on
  (`judge.output_guard_llm`), the advisory after a flagged tool result carried the judge's
  reasoning and its free-form flags: text the judge wrote after reading the attacker-controlled
  output, delivered with operator-level trust. The advisory now names what the judge found with
  symbols from a fixed vocabulary, each with one fixed sentence, and a flag outside the vocabulary
  shows as `unclassified`; the judge's prompt lists the same vocabulary. The chip and the audit
  rows keep the judge's reasoning and flags as it wrote them, and findings from the regex stage
  read as before. Workstreams created before the upgrade keep the advisories already stored in
  them, judge prose included.
- **The model's own words replay as written (#1291).** The fold of operator turns, the shared
  workstream's sender-label pass and the reasoning replayed to a vLLM model defanged fence markers
  in the model's own text and reasoning, so the model read its history rewritten. They now clean
  only what comes in (tool results, attachments, other participants' messages) and replay the
  model's text, reasoning and tool-call arguments as it wrote them. The summarizer that writes a
  compaction summary reads every turn with fence markers defanged and the session's tokens removed,
  the model's own included, since another model reads them and nothing it reads is replayed; its
  summary is kept as it wrote it. What the framework writes from outside into an assistant turn (the
  last user message a summary quotes, a child workstream's name, a hosted search's citations footer)
  or a system turn (a message queued while the model works) is cleaned where it is composed; the
  model's own last turn, which a compaction carries verbatim, stays as written. A hosted search's
  results and citations replay inside the model's turn as the provider returned them, beyond any
  text pass, like text inside an image or a natively read PDF; the declaration now says a block
  inside one of the model's own earlier turns, its search results and citations included, is never
  the operator's. Anywhere else, text from outside never carries a session token in, so a block with
  one in the model's history is one the model wrote itself.
- **Task agents are told what the output guard found (#1291).** A task agent's tool results were
  scanned and redacted, but its model never saw the finding, so a sub-agent that read a prompt
  injection got no warning even though the guard flagged it and the operator's chip showed it. A
  flagged result now gets the same advisory as in the conversation, after the step's tool results,
  and the agent's intent judge sees it on later gated calls. A tool's own text inside a list result
  is now guarded; list results skipped the guard entirely. The only list result today, `read_file`
  on an image, holds just the framework's header before the image. An agent whose model takes the
  advisory in the trusted fence, under a main model that takes native system messages, now has that
  fence declared in its prompt. The guard reads past the agent's 16,000-character cut so it can
  redact a credential that straddles it, so the advisory for a result the cut shortened says the
  finding may concern the part the agent did not receive. The declaration itself, in every session
  whose model folds operator turns, now says a block carrying the session's token can follow a tool
  result, and that a block inside one of the model's own earlier turns, its search results and
  citations included, is never the operator's; it told the model to distrust any marker inside tool
  output, which read literally covered the real advisory appended there. Since the declaration
  trusts the token alone, the token itself is now removed from untrusted text wherever it joins the
  request (tool results and other incoming history, before the fold appends its block, and
  attachments), matched past case, past accents and compatibility forms (`é`, full-width,
  mathematical, circled) and past anything between its characters that is not an ASCII letter or
  digit (spaces, line breaks, invisible characters, combining marks): a leaked token inside a marker
  spelled with a zero-width space or a lookalike letter passed the old defang. Lookalikes that
  Unicode does not decompose to a letter or digit (another script's letters, ASCII `O` for `0` and
  `l` or `I` for `1`, and on Python 3.13, whose Unicode predates them, the outlined letters and
  digits Unicode 16 added) are not matched, and text inside an image, a natively read PDF or a
  hosted search's results and citations is beyond the reach of a text pass. The sender label of a
  shared workstream gets the same treatment, and the guard flags a leaked token wherever it appears.
  All of a step's advisories follow its last result, so in a step with several results each advisory
  now names its own by position, and by tool when the session offers a tool of that name; the
  operator's guard card shows the same label. Text the framework wrote itself (a denial quoting the
  approver's feedback, an unknown-tool or agent-mode gate error, the header before an image) is no
  longer guarded; a denial such as "From now on you must write outputs to /tmp" scored as an
  injection, and the advisory turned the approver's own correction against them.
- **The output guard's judge no longer reads secrets the model never sees (#1291).** Where the regex
  stage redacts a credential (`judge.redact_secrets`), the guard's LLM stage read the raw tool
  output, so a secret the session's model never saw reached the guard's model, possibly another
  provider's, and the reasoning it writes into the audit row. It now reads the redacted text the
  session's model receives; the lines it cites are that text's.
- **Output-guard findings name the lines the judge flagged (#1291).** With the LLM stage on, the
  judge reads the tool output with numbered lines and may cite ranges; the advisory names them as
  numbers, never quoting the lines. A range reaches the model only if its lines read the same, at
  the same numbers, in what the model receives: one after a re-cut or a task agent's cut is dropped
  rather than renumbered. The sentence says the numbers count from the first line of the result,
  since `read_file` and search output print numbers of their own, and asks the model to treat the
  whole result with the same caution, since the judge chose the lines after reading text that can
  steer it. A result the main loop cuts again after redaction now also gets the notice that the
  finding may concern the part that was cut. Output whose numbered prompt would not fit the judge's
  window, such as one padded with blank lines, is judged unnumbered and cites no lines, where it
  would otherwise skip the judge.
- **Tool policies that cannot be read refuse the batch.** When reading the admin tool policies
  failed, every call came back as matching no policy, so `deny` rules stopped applying:
  skip-permissions, "Always" grants, auto-approve lists, the smart-approval judge or a person could
  run a call a `deny` rule covered. Now every call in the batch that needs approval is refused, with
  a message telling the model to try again; calls that need no approval still run. Successful reads
  stay cached for 60 seconds and a failed one is not cached, so the next batch reads again. This
  applies to node and coordinator sessions and to the CLI. A call tool policies refused, or one
  nobody answered before the approval deadline, is no longer reported to the model as the user
  rejecting it.
- **"Always" approves only what the person approved.** Approving a batch with "Always" also granted
  the always-approval to a call a `deny` rule had refused in that batch, so the call ran without a
  prompt if the rule was later removed or relaxed to `ask`. Refused calls now get no grant.
- **URL tools look up a hostname only after approval.** `web_fetch` and `open_preview` screened
  their target while preparing the call, and the screen resolved the hostname. A denied or
  cancelled call had already sent that name to DNS, and a private or unresolvable answer was
  returned to the model without any approval. Preparation now judges only what needs no lookup:
  malformed URLs, metadata hostnames, and IP literals, which keep their current refusals and
  private-network marking. A hostname is resolved and screened when the approved request is made,
  and refusals keep their wording. With `tools.allow_private_network` on, a hostname that turns
  out to be private is refused once with a request to repeat the call; the repeat is marked as
  a private-network request, and approving it grants access. Tagged requests in task agents
  now use the same approval gate and policies as the conversation. A granted hostname that
  no longer answers wholly private is refused and forgotten; if it now answers only public
  addresses, a retry can be approved as an ordinary request. The grant recheck and first fetch
  hop share one DNS answer. Each hop connects only to its screened addresses, closing the
  screen-to-connect rebinding gap while preserving the hostname for HTTP routing, TLS SNI,
  and certificate verification. Proxy-routed requests are refused because a proxy could
  resolve the destination independently; `NO_PROXY` exclusions can connect directly.
- **Credential redaction covers JSON Web Tokens and credential query parameters.** A JWT after
  `token=` or `access_token=` lost only its header to redaction, leaving its payload and signature,
  and a query parameter such as `?access_token=ya29.…`, `?client_secret=…` or `?password=…` was not
  redacted unless its value was 20 or more letters and digits. The output guard, its configurable
  built-in rules (`credential_jwt`, `credential_query_param`), and the browser's display redaction
  now redact a JWT whole where it stands on its own, also right after a percent-escape or a `\u`,
  `\n`, `\r`, or `\t` escape, as inside an encoded URL or an escaped string, and a query or fragment
  parameter named `token`, `access_token`, `refresh_token`, `id_token`, `auth_token`, `api_token`,
  `session_token`, `bearer_token`, or `secret_token` (also without the underscore), `api_key`,
  `secret`, `client_secret`, `password`, or `passwd` to the end of its value once it is at least 8
  characters, also after an escaped `&amp;` or `\u0026` or percent-encoded inside another URL. Both
  run before the key-prefix rules, so a token or query value that starts with a recognisable key is
  still redacted whole. Bare `key` and `auth` parameters are still left alone, and a pagination
  cursor passed as `?token=` is now redacted from tool output too. Audit details, persisted errors,
  and tool-argument previews use the same redaction.
- **Connection-string redaction stays fast on nested URLs.** Every `https://` in a long run of
  nested URLs rescanned the rest of the run for an `@`, so redacting 128 KB of such tool output took
  8.5 seconds, and the time grew with the square of the size. The output guard and the browser's
  redaction now end a URL's password where another connection string could begin, which keeps the
  scan linear and still redacts a password of any length, `/` and `://` included, unless the
  password itself contains one of the recognised schemes, such as `https://`.
- **The web UIs no longer fetch fonts from a third-party host.** The node UI, console and
  coordinator pages loaded their two typefaces from an external font service on every page load.
  Each load sent that service the viewer's IP address, user agent and the deployment's origin,
  and the page stayed blank until the request finished or failed, which takes the full connection
  timeout where outbound traffic is silently dropped. The same font files now ship in the package
  (`shared_static/inter-4.001/`, `shared_static/jetbrains-mono-2.211/`) and are cached like the
  other version-named vendor assets. Pages look the same where the service was reachable; where
  it was blocked, they now get the intended typefaces instead of system fonts.

## [1.8.3]

Turnstone 1.8.3 makes scheduled work easier to launch, keeps conversations and node placement intact
across restarts, strengthens sign-in and session permissions, and improves the everyday browser
controls. It also adds GPT-6 Astra support and downloadable previews.

> **Before upgrading:** this release advances the database schema from migration 072 to 076, adding
> schedule time zones, correcting stored one-shot times, and recording channel owners and required
> execution nodes. Migrations run automatically. Existing recurring schedules keep their UTC timing;
> edit their time zone to switch to local time. Older Discord threads without a saved invoker need
> a new `/ask` conversation. An administrator must assign roles to local accounts that have none.

### Added

- **Scheduled tasks from the dashboard (#1090).** Choose Scheduled in the launcher, enter the task,
  and pick Daily, Weekly, Monthly, Interval, Once, or Cron. The launcher and Admin Schedules share
  a timing builder showing upcoming runs in your local time; saving confirms the first run.
- **Recurring schedules in your time zone (#1091, #1097).** Schedules created in the browser use its
  time zone and retain it when edited. Daily, weekly, and monthly times follow daylight-saving
  changes, with a fixed time firing once during the repeated fall-back hour. The API and both SDKs
  accept a time zone. Invalid zones and impossible dates are rejected with an explanation.
- **GPT-6 Astra.** The OpenAI provider recognizes `gpt-6-astra`, including its context and output
  limits, reasoning levels, vision, PDF input, tool search, and mid-conversation instructions.
  Responses history also preserves the distinction between commentary and final answers, and their
  order around tool calls, independently of whether reasoning replay is enabled.
- **Visible approval reminders.** A persistent status-bar chip counts pending approvals and reveals
  the waiting card, including cards inside collapsed task agents. Coordinators also have a clickable
  approval count for child workstreams.
- **Preview downloads.** Download the original file from a preview, including the complete table
  when the displayed data is sorted or capped. Downloads retain the source filename and stay tied to
  the originating workstream and node after navigation or reload.

### Changed

- **Clearer pane controls.** Each tab has a dismiss button: `−` hides a regular split pane while
  keeping its tab; `×` closes a closable tab or preview. A chevron opens the pane menu with mouse
  or keyboard. The active tab and pane agree visually, hidden tabs remain closable, and controls
  stay visible when the tab strip overflows.
- **Reasoning progress with elapsed time.** Interactive and coordinator views share a Reasoning
  indicator and live clock in both Default and Compact modes. The clock measures what the browser
  observed; restored history does not invent a duration.
- **More readable light-theme indicators (#1092, #1094).** Darker accent colors improve contrast for
  launcher labels, badges, buttons, and approval reminders.

### Fixed

- **Schedule dispatch and completion (#1099).** A firing is recorded as successful only after a node
  creates the workstream. Definite failures retry about once a minute for five minutes, keeping
  that deadline across console restarts. Uncertain results and partial fan-out success are reported
  without retrying jobs that may already be running. One-shot status reflects whether the run
  happened, including after re-arming or conversion from cron.
- **One-shot times and schedule validation (#1096, #1098).** One-shot schedules honor their UTC
  offset, including schedules stored before upgrading. Explicit null fields are rejected with
  the field named, preventing accidental disabling or names saved as `"None"`.
- **Discord and Slack conversation recovery (#1067).** Restarts, idle eviction, and missing event
  streams no longer discard a channel's saved conversation. The next authorized message restores its
  history and subscription. Recovery uses the exact saved identity and preserves the route when a
  node is temporarily unreachable; concurrent recovery cannot overwrite another replacement.
- **Persistent node placement (#1106).** A workstream explicitly assigned to a node stays there
  after restart, close, idle timeout, or eviction. Reopening and routing honor that requirement
  instead of silently moving execution. Forks inherit it unless a new destination is chosen.
- **Reopening without an extra turn.** Restoring a workstream no longer launches a redundant
  memory search. A message sent during reopening starts its own turn.
- **Empty model completions (#1070).** A response with no answer or tool call now retries within a
  bounded budget when no server-side tools may have run. Otherwise it reports an actionable error
  instead of silently completing. Provider refusals remain visible.
- **MCP timeouts (#951).** A caller's wait deadline cancels its local waiter without marking the
  server unhealthy or opening its circuit breaker. Server-reported timeouts still count as failures.
- **MCP OAuth registration and refresh (#1081, #1082).** Servers keep their resolved issuer when
  discovery metadata comes from cache, so token refresh uses the right issuer. Client registration
  no longer sends an unrelated resource parameter that authorization servers may reject.
- **Shared Docker authentication setup (#1062, #1064).** Both Docker stacks can mount one private
  `config.toml` for the console and nodes, sharing the encryption key and callback configuration for
  MCP OAuth. The installer can prepare it once and preserves it on reruns. The guides cover local
  login, SSO, delegated access, and remote callback registration.
- **Operator access to saved history.** Authorized project members can list and reopen interactive
  history without coordinator administration permissions. Read-only users can inspect and export
  history; reopening or deleting requires write access. Nodes also enforce project and coordinator
  permissions when requests arrive directly or through the console.
- **Sign-in recovery on page load.** Rejected or outdated credentials reliably return the browser to
  sign-in, including when the initial identity check races other requests. An older response cannot
  erase a newer authenticated session.

### Security

- **Session creation and renewal use current permissions.** Password and OIDC sessions require
  active role permissions. Permission-store outages return a retryable error without extending stale
  permissions or clearing the existing cookie. Only human password/OIDC sessions can renew through
  the public refresh endpoint. API-token sessions retain their explicit scopes and fixed expiry;
  login no longer exchanges JWTs for fresh sessions. `turnstone-admin create-user` assigns viewer
  access explicitly.
- **Discord thread ownership survives recovery.** Only the original linked invoker can continue or
  close the conversation. Another linked user cannot claim a bot-owned thread.

## [1.8.2]

Turnstone 1.8.2 moves model endpoints entirely into model definitions and
retires the server's bootstrap endpoint flags, fixes MCP OAuth discovery for
servers and issuers whose identity carries a path, teaches `web_fetch` to read
PDFs, and gives every tool result one honest size limit. It includes no
database schema migrations. Operators who start `turnstone-server` with
`--base-url`, `--api-key`, `--provider` or `--model`, or who still carry an
`[api]` section in `config.toml`, should read the **Removed** section before
upgrading.

### Added

- **Claude Fable 5.1 (#1076).** The Anthropic lane knows `claude-fable-5-1`:
  1M context, 128K output, adaptive thinking, the full effort ladder, web
  search, tool search, vision, PDF and mid-conversation system messages. New
  Anthropic organizations reject replayed thinking that predates an edit to
  the conversation prefix; Turnstone opts into the documented degrade mode,
  so such blocks are dropped server-side and unbilled and the request
  proceeds instead of failing.
- **PDF support in `web_fetch` (#1055).** A fetched response that is a PDF
  is read the same way an attached PDF is: natively when the active model
  accepts documents, as page images for a vision model, through configured
  perception, and otherwise by local text extraction in an isolated worker
  bounded by memory, CPU, wall time and output size. When none of those can
  read the document the tool returns an explicit error instead of answering
  from an empty page. Fetched PDFs are request-local and never enter
  conversation history or attachment storage. Hardened containers need a
  writable `/tmp` and a seccomp policy that permits the worker's
  process-and-limit syscalls; see `docs/tools.md`.
- **MCP OAuth discovery for servers on a private network.** The new
  `mcp.oauth_allow_private_network` setting (console Settings → MCP,
  default off) lets an MCP server at an internal address complete OAuth
  discovery. It applies only to what the operator typed on the row — the
  server URL and the authorization-server override — so an authorization
  server named by a fetched document is still refused when it is private,
  and a remote server cannot reach into the deployment's own network. Cloud
  metadata, link-local, multicast and reserved addresses stay refused, and
  per-user bearers still require `https://`. A refusal names the setting
  that would allow it.

### Changed

- **One size limit for tool results (#1075).** Every place a tool result,
  web page, search snippet, skill clip or reasoning display was cut now
  uses the same rule: the limit includes its marker, and the marker
  reports how much the producer actually returned rather than blaming the
  limit. In automatic mode each result receives 20% of the batch's
  remaining input budget, capped at 20% of the context window, and the
  `tools.truncation` help text says so. Long `bash` and `diff_file`
  output keeps its head and tail edges rather than buffering the whole
  capture. A `bash_output` read returns whole lines up to the cap and reports
  how many remain unread for the next call. A stored setting outside a
  range the registry has since tightened is clamped on load with a
  warning, and shown clamped in the admin listing, instead of silently
  reverting to the default.
- **A model definition that leaves `api_key` empty on a local server**
  (`openai-compatible` / `anthropic-compatible`) resolves its bearer the way
  the server's old `--api-key` default did: the SDK's environment variable
  when set, otherwise a placeholder, so vLLM and llama.cpp definitions keep
  working without an exported `OPENAI_API_KEY`. Commercial providers keep
  the SDK environment-variable rule unchanged.
- **`turnstone-doctor` resolves its own model the way a node does**, from
  the database definitions and `[models.*]`, instead of seeding one from
  `[api]`, which a node no longer reads.
- **A `[models.*]` entry that names neither `base_url` nor `provider`** is
  skipped with a startup warning. It used to inherit the server's bootstrap
  endpoint; without one, the default provider would have sent its prompts
  to the commercial OpenAI API.

### Removed

- **`turnstone-server` no longer takes `--base-url`, `--api-key`,
  `--provider` or `--model`, and no longer reads `[api]` from
  `config.toml` (#1052).** Model endpoints live in the console Models tab
  and in `[models.*]` entries, each with its own `base_url` and `api_key`.
  A server started with nothing configured boots with an empty registry
  and picks models up live from the console. `compose.yaml` drops the
  `LLM_BASE_URL` and `MODEL` variables, the Helm chart drops
  `llm.baseUrl` / `llm.provider` (nothing read their environment
  variables), and the Terraform module drops `llm_base_url`. A
  `config.toml` that still carries `[api]` gets a startup warning naming
  the replacement. The `turnstone` CLI keeps its flags for now (#1085).

### Fixed

- **Context window auto-detection (#1052).** A model definition whose
  `context_window` is `0` used to inherit whatever window the server had
  detected on its bootstrap endpoint at boot: 32768 whenever `--model` was
  passed or that endpoint was unreachable, and the wrong endpoint's number
  otherwise. It is now resolved per definition, at startup and on every
  hot-reload: from the provider capability table for Anthropic, OpenAI and
  xAI, and by asking the definition's own endpoint for local servers and
  gateways (vLLM, llama.cpp, OpenRouter, Together, Fireworks, Groq and
  Mistral all report it). Identical definitions share one probe, probes run
  concurrently under a five-second budget, and when a reload's probe fails
  the window the running registry already detected is kept, so a backend
  mid-restart never shrinks live sessions. A definition that cannot be
  resolved falls back to 32768 with a startup warning naming the alias and
  the reason. The server, the console and the doctor resolve identically.
  The console Detect button no longer offers a commercial model's window
  for a local server, and when the endpoint reports none it fills in the
  32768 the node would use and says so. A definition created through the
  admin API without a `context_window` is stored as auto-detect rather than
  a literal 32768, a local-server definition without a `base_url` is
  refused at save time, and node hot-reloads are serialised and keep the
  running registry when a load fails.
- **MCP OAuth discovery for servers and issuers with paths (#1061, #1063).**
  A server or authorization server whose identity carries a path never
  resolved, because metadata URLs kept only the origin. Protected-resource
  metadata is now tried at the RFC 9728 path-specific location first and
  the origin-level location second, following one `WWW-Authenticate`
  challenge per location, and each document must declare the identifier
  its own URL was derived from. Authorization-server metadata is tried at
  the locations MCP lists, and a candidate wins only after its whole
  document validates, so a catch-all response cannot shadow the document
  that describes the issuer. A document whose `issuer` does not match the
  requested one is skipped; the templated issuer that multi-tenant
  providers publish is accepted and logged. Discovery runs under one
  wall-clock budget. User-scoped server rows store the canonical URL, so a
  spelling-only change no longer purges user tokens, and every MCP OAuth
  request asks for JSON so a content-negotiating token endpoint returns it
  on every grant leg.
- **Changing a server's authorization server now purges its grants.**
  Pointing a user-scoped MCP row at a different Authorization Server URL
  left every per-user grant and pending consent in place, so the next
  refresh presented tokens issued by the previous server to the new one.
  The change now purges those rows, so users re-consent against the server
  now configured, and clears a dynamically registered client id the
  previous server issued. A pre-registered client id is left alone.
- **CLI startup failed with a retired-workstream error (#1058).** The
  interactive `turnstone` CLI reserved a workstream id and then built its
  session under a different one, so a fresh session could die during
  construction with `workstream ... was retired`. The reserved id is now
  carried through, and a subprocess test starts the CLI against a fresh
  database on every run.

## [1.8.1]

Turnstone 1.8.1 strengthens long-running agent context and stable dependency
compatibility, adds a quieter transcript view, and makes operator waits
explicit. It includes no database schema migrations.

### Added

- **Task-agent compaction (#1044).** Long-running agents now receive the same
  soft warning, hard compaction, recursive overflow recovery, and visible
  progress as the foreground while retaining bounded execution evidence for
  cancellation disposition and task recall. The generalized `compaction`
  lifecycle targets either the workstream or a parent task card; task
  compaction is transient and never persists the agent's private summary.
  Python and TypeScript SDKs expose the target fields.
- **Compact transcript presentation (#1048).** A browser-local Default/Compact
  control now spans interactive and coordinator views. Compact mode hides
  completed reasoning and folds fully settled routine tool batches while
  keeping active, exceptional, and task-agent work visible; live events and
  restored history use the same presentation rules.

### Changed

- **Configurable tool-approval waits (#1038, #1039).** The new
  `tools.approval_timeout_seconds` setting defaults to `0` for an indefinite
  operator wait and accepts positive finite deadlines. Each approval batch
  snapshots the live value, platform wait limits are respected, and hard
  deletion still releases outstanding waits.

### Fixed

- **Memory admission during MCP catalog priming (#1046).** First-turn immutable
  memory-index capture now tolerates several asynchronous resource or prompt
  catalog invalidations with a bounded, cancellation-aware backoff outside the
  storage transaction and model-capacity lease.
- **Anthropic SDK major-version guard (#1049, #1050).** Stable installations now
  require `anthropic>=0.117,<1`, preventing fresh resolution of the HTTPX2-based
  v1 SDK before Turnstone's client, sampling, and transport boundaries are
  deliberately migrated.

## [1.8.0]

Turnstone 1.8 focuses on dependable long-running workstreams: one model-call
pipeline, lossless reconnect and history recovery, visible compaction, stronger
memory boundaries, and expanded enterprise identity and deployment paths. This
entry describes the change from 1.7.4; features already shipped on
the 1.7 line are not repeated here.

> **Before upgrading:** 1.8 advances the database schema from migration 066 to
> 072. The migrations add encrypted OIDC credential storage, per-model
> authentication and concurrency fields, idempotent conversation commit keys,
> and immutable memory-index snapshots. Migration 072 also removes the obsolete
> `memory.fetch_limit` setting and resets structured-memory access counters
> because access now means an explicit full-body fetch. Migrations run
> automatically, but back up the database before upgrading as usual.

### Added

- **Immutable memory-index snapshots (#902).** At the first real model call, a
  workstream captures the complete memory metadata visible to its acting user
  and project. That index stays cache-stable for the life of the workstream;
  later turns receive compact relevance pointers while memory bodies remain
  behind an explicit `memory_get`. The console and SDKs expose snapshot health,
  and project, persona, approval, and principal boundaries are enforced
  throughout. Indexes over 65,536 characters raise an operator warning rather
  than being truncated; task agents remain memory-disabled in this release.
- **Single-sign-on passthrough for MCP servers (#551).** The new
  `auth_type="oauth_obo"` can reuse an opt-in credential captured at Turnstone
  OIDC login to mint short-lived, audience-scoped tokens for any configured MCP
  server. It supports Entra and RFC 8693 exchange profiles, encrypted
  credential storage, rotation, revocation, cache flushing, admin UI, and
  operator documentation without changing existing `oauth_user` flows.
- **Per-model gateway identity (#898, #950, #955).** A model alias can retain its
  static key or explicitly use caller-delegated Entra OBO, RFC 8693 token
  exchange, or shared app identity. Dynamic authentication is default-deny at
  the admin boundary, uses audience and scope allow-lists, follows registry
  reloads safely, and fails closed where no valid caller or fallback exists.
  Existing aliases remain `static`.
- **Visible, durable compaction.** Manual and automatic compaction now emit a
  typed `compaction` lifecycle event and render a progress/result card in both
  web viewers. The final token delta and summary survive reload through
  history. Slash commands render as command chips, run on the workstream worker
  slot, and defer concurrent sends in order instead of truncating or losing
  them; Stop cancels an in-flight summary request.
- **Coordinator MCP and unfinished-work recovery (#725, #913).** Coordinator
  workstreams can use MCP tools, resources, and prompts under the same persona
  and user gates as interactive sessions. Coordinators that go idle with
  running children or open tasks receive bounded, factual nudges; tasks gain a
  `needs_user` state and note, and task/child handles survive compaction.
- **Optional project-required creation policy.** Enabling the default-off
  `server.require_project` setting requires human-created interactive and
  coordinator workstreams to be filed under a project. Forks inherit their
  source project, while service automation and coordinator-spawned sessions
  stay exempt. The console and direct API enforce the same tenancy-safe rule.
- **Per-alias model concurrency limits (#917).** Operators can set
  `max_concurrency` on a model alias. Admission is FIFO, covers the whole
  stream, participates in request deadlines, and defaults to unlimited for
  existing aliases.
- **Large text pastes become attachments (#930).** Pasting more than 2,000
  characters stages `pasted-text.txt` across every attachment-capable
  composer. Clipboard files retain priority, text above the 512 KiB attachment
  ceiling stays inline, duplicates collapse, and refused or queued sends keep
  their staged content for retry.
- **Copy actions across every chat surface.** Assistant messages copy their raw
  Markdown, while code, Mermaid, and table blocks can be copied by pointer or
  keyboard. The fallback works on plain-HTTP LAN deployments and preserves
  focus and selection.
- **Task-agent context meters (#982).** Running task-agent cards show normalized
  used/total context after model turns that report usage, warn at 80%, recover
  on reconnect, and clear on every terminal path. Python and TypeScript SDKs
  gain the typed `agent_context` event.
- **Claude Opus 5 support.** The Anthropic capability table includes its 1M
  context window, 128K output, adaptive thinking, and full effort ladder.
  Provider refusals now surface as content-filter stops instead of successful
  empty answers.
- **`server_parses_reasoning` model capability.** Models whose server already
  separates reasoning can disable Turnstone's inline tag scanner on every lane,
  preventing quoted `<think>` text from being mistaken for hidden reasoning.
  Commercial provider defaults declare this capability automatically.

### Changed

- **All model calls use one turn and transport pipeline (#827, #831, #832,
  #979).** Interactive turns, judges, titles, compaction, perception, web
  extraction, evals, optimizers, and task agents now share `model_turn`,
  provider-native replay, one lowering/re-ingest path, and the streaming
  provider entry point. Utility calls are complete-or-error, cancellation closes
  the underlying read, and transient stream failures retry at the shared
  boundary.
  - This is a compatibility boundary: every configured endpoint must support
    streaming, and OpenAI-compatible endpoints must accept the current
    `stream_options`. A gateway that cleanly ends a stream without a terminal
    marker needs
    `{"finish_reason_optional": true}` in that model's capabilities.
  - The non-streaming `create_completion` adapter method and the unused
    `supports_streaming` capability are gone.
- **Sampling settings inherit instead of manufacturing defaults.** Temperature
  and reasoning effort now resolve as alias override, then stored global
  setting, then model-declared effort default, then omission. New workstreams
  therefore inherit each backend's own defaults instead of silently sending
  `temperature=0.5` and `reasoning_effort="medium"`. Use `none` to disable
  reasoning explicitly; an empty effort now means inherit. Existing saved
  workstreams retain their stamped values until their model or sampling
  settings change.
- **Compaction feedback is a typed event.** SSE and SDK consumers must handle
  the `compaction` lifecycle event; 1.8 no longer dual-emits those updates as
  generic `info` lines, which would double-render them in current clients.
- **OpenAI SDK v3 and HTTPX2 are the supported provider stack (#1009).** Chat
  Completions and Responses normalize HTTPX2 and legacy injected-HTTPX failures
  through the same retry boundary. OpenAI's default client now uses the
  operating-system trust store; installations that modified only `certifi`
  must install their CA system-wide or set `SSL_CERT_FILE` /
  `SSL_CERT_DIR`.
- **lacme 1.2 and HTTPX2 back core mTLS (#1011).** Consoles can advertise a
  routable `TURNSTONE_ACME_EXTERNAL_URL` for cross-host enrollment. Enrollment
  uses purpose-confined rotating JWTs, managed identities are validated before
  reuse, failed renewals keep the last usable certificate, and typed IPv4/IPv6
  identifiers are issued as IP SANs. A complete multi-process cross-host
  Compose smoke remains a manual deployment check.
- **Retrieval and evaluation share bounded runtimes.** Reranking reuses a
  lifecycle-owned HTTP client, the selected alias's admission limit, and a
  circuit breaker that preserves native retrieval order during outages
  (#1027). Intent-judge batch evaluations run in parallel rather than serially
  (#946).
- **File tools tell models where they operate (#857).** The `bash` and file
  tool descriptions include the process working directory and, when present,
  the user workspace directory. Stock Docker installs identify `/workspace`
  without changing path permissions or the process cwd.
- **Streaming log rename.** `drain_stream.post_finish_blip` is now
  `stream.post_finish_blip`; the `usage_captured` field is unchanged.
- **Frontend and SDK dependencies were refreshed.** The Anthropic SDK floor is
  0.117, the bundled renderer now carries KaTeX 0.18.4, Highlight.js 11.12.0,
  Mermaid 11.16.1, and HLS.js 1.7.0, and the TypeScript SDK moves to TypeScript
  7.

### Removed

- **Retired OpenAI capability rows.** Built-in rows for the o1/o3/o4 families
  and GPT-5 through GPT-5.3 have been removed; the built-in floor is GPT-5.4.
  Unlisted gateway models still use generic commercial defaults and can declare
  an explicit capabilities JSON.

### Fixed

- **Lossless SSE and history recovery.** Truncated replay now disconnects,
  rebuilds history, adopts the returned cursor, and reconnects without dropping
  an in-flight turn. Tool-output chunks are coalesced, restart-era global
  cursors carry a boot epoch, concurrent `/history` reconstructions coalesce,
  and both interactive and coordinator panes preserve their transcript across
  failed refreshes, stale rewind windows, hidden-tab reconnects, and teardown.
- **Durable history handoff and deletion safety (#981).** Conversation writes
  carry idempotency keys, retry ambiguous acknowledgements without duplicates,
  and refuse to cross a deleted-workstream boundary. History fails closed with
  503 rather than painting an empty transcript when persistence is unresolved;
  queued interjections remain partitioned by owner, and operator persistence
  state no longer reports a tombstoned session as healthy.
- **Provider failures are classified before they corrupt a turn.** Mid-body
  transport deaths retry without double-rendering; HTTP-200 JSON error payloads
  from OpenAI-compatible endpoints surface their real rate-limit/server
  classification with bounded, cancellable, redacted reads (#995); Responses
  truncations and refusals preserve usage and content; and a call cancelled
  before dispatch no longer mints credentials or sends a request.
- **Reasoning stays in the reasoning lane (#940, #965).** Inline
  `<think>`/`<reasoning>` blocks are split once at the shared seam, split tags
  reassemble across deltas, the active fallback model controls scanning, and
  unmarked chain-of-thought is suppressed for titles, summaries, and web
  extraction. Multi-block notification text is separated instead of fused.
- **MCP catalogs remain live and honest.** Static `list_changed` notifications
  no longer deadlock their own receive loop; pooled catalogs survive session
  eviction; refreshes are serialized, bounded, retried, and reported accurately;
  and either half of resource discovery may be unsupported without discarding
  the other (#839, #993). Tool search now reports unavailable servers and the
  full pre-limit match count rather than pretending a discovery failure was a
  genuine no-match (#938).
- **Workstream state cannot wedge behind lifecycle edges.** A failed worker
  thread start releases its slot, first-message failures remain in the error
  state, zero-budget truncation keeps tool results valid, force-cancel runs
  abandonment before publishing idle, operator skill changes are recorded, and
  soft close wakes pending web and background-CLI approvals before waiting for
  cleanup (#1013).
- **Frontend assets stay fresh across same-version deployments (#1014).**
  First-party assets use content-derived ETags and revalidation, versioned
  vendor files remain immutable, coordinator and proxied node responses preserve
  cache policy, and encoded traversal paths are rejected. Composer project,
  persona, model, and skill selectors paint immediately from their warm caches;
  proxied node views also restore their back-to-console link.
- **Memory authorization and storage parity.** Project-scoped memory operations
  bind to the immutable acting principal, omitted-scope get/delete follows the
  workstream's attached project, writes are atomic, and new memories require a
  useful description. Fresh databases now create the same structured-memory
  indexes as migrated databases (#1028).
- **Helm and Docker deployment paths work as documented.** The chart can install
  with bundled or external PostgreSQL, renders each credential independently,
  gives every server replica a routable per-pod address, and exposes scheduling
  controls. Docker Compose health checks work under WSL.

### Security

- **Outbound URL guards classify the destination actually reached
  (GHSA-wm4f-79pw-pfr9).** One shared policy now handles NAT64, 6to4, Teredo,
  mapped IPv4, CGNAT, metadata endpoints, DNS failures, and every redirect hop.
  Private-network opt-ins still permit home-lab addresses but never metadata,
  link-local, multicast, unspecified, or reserved destinations.
- **Untrusted channel fields cannot create mentions.** Discord mention
  resolution is disabled for model-authored messages, and Slack fields are
  escaped at their trust boundary.
- **Dynamic model authentication is permission-gated and fail-closed.** Enabling
  or changing an identity-bearing alias requires the dedicated admin authority,
  validates its audience/scope posture, and never silently falls back between
  delegated and app identities.

### Documentation and verification

- The API reference, SDK guides, architecture diagrams, MCP OBO guide, memory
  guide, deployment docs, and TLS runbook were updated for the 1.8 contracts.
- New opt-in harnesses exercise OBO minting, frontend asset revalidation, and
  end-to-end SSE recovery. `scripts/recovery_e2e.py` covers interactive,
  coordinator, restart, truncation, and teardown scenarios in a real browser.
- Release development and GitHub's default pull-request base now use `dev`,
  while `main` remains the current stable install source and prior `stable/X.Y`
  fixes merge forward. CI, Renovate, the installer, the release helper, and
  publication aliases enforce that topology.
- The README now leads its harness explanation with an architecture diagram,
  and the contributor roster includes the latest external contributors.

## [1.7.4]

A feature-bearing patch for the 1.7 line, rolling up work that had stabilised
on `main`. No schema migrations (head stays 066) and no new configuration knobs.

### Added

- **Background shells for the `bash` tool** — `run_in_background=true` starts a
  command as a detached shell and returns a `bash_N` handle; new `bash_output`
  (delta output since last read, optional regex filter, status/exit code) and
  `kill_shell` (terminates the shell's process group) tools manage it. Output is
  buffered with a drop-oldest cap, a system notice lands when a shell exits, and
  shells die with their workstream — never outliving a `task_agent` that started
  them.
- **`task_agent` carries the model's native reasoning across its own tool loop** —
  a task agent's replayed turns now preserve the provider-native reasoning lane
  (Anthropic thinking blocks with signatures, OpenAI reasoning items, Gemini
  `thought_signature`, vLLM/llama.cpp reasoning text) instead of rebuilding each
  turn from text alone, restoring reasoning continuity for thinking models.
- **Model-shelf response controls** — the console model shelf exposes verbosity
  and reasoning-mode controls per identity.

### Changed

- **GPT-5.6 aligned with the GA API surface** — the Responses provider matches
  GPT-5.6's GA shape (typed `reasoning.mode`, `prompt_cache_options`,
  cache-write accounting); the `openai` floor moves to `>=2.45`.

### Fixed

- **`bash` never hangs on a backgrounded child** — a command that left a
  long-lived process running no longer wedges the workstream; the tool waits on
  the tracked process (bounded by the timeout) and reaps its whole process group.
- **`task_agent` sub-tool ids are session-unique** — ids are minted
  `{parent}::r{run}s{step}::{id}` so a local model reissuing sequential ids
  (`call_0` each turn) no longer aliases two steps onto one live-card row while
  `/history` keeps them apart.
- **Judge completions honour model-definition capabilities** — a judge's
  completion now threads its model's declared capabilities instead of assuming a
  default surface.
- **`create-admin` CLI** — adds an explicit admin-creation command; `run.sh` no
  longer onboards into a role-less user.
- **Install script Docker handling** — installs Docker on distros
  `get.docker.com` rejects, and gates that path by `$ID` instead of trapping all
  failures.

## [1.7.3]

A small feature and maintenance patch for the 1.7 line. No schema migrations
and no new configuration knobs.

### Added

- **OpenAI GPT-5.6 (Sol/Terra/Luna) support** — the Responses provider
  understands the GPT-5.6 family: the `reasoning.mode` control, the new
  `max` effort tier, and `text.verbosity`, with golden wire payloads pinning
  the request shapes. The `openai` dependency floor moves to `>=2.44`.

### Changed

- **Engineer base prompt hardened with process discipline** — the default
  base prompt for non-coordinator sessions now works in phases scaled to the
  size of the change, defaults to red-green for testable work, scopes to the
  smallest sufficient diff, stops to report after repeated failed attempts
  instead of thrashing, reports only observed results, and delegates
  exploration to `task_agent`. Persona prompts freeze into the workstream
  stamp at creation, so this reaches new workstreams only.

### Fixed

- **Unknown reasoning-mode warnings name the allowed modes** — a model
  definition with an unrecognized reasoning mode now logs the valid options
  instead of leaving the operator to guess.

### Documentation

- **HYPOTHESIS.md / PRIMER.md** — the control normal form is tightened and
  the factored Q_E reading is carried into the glossary; the plain-language
  PRIMER stays in sync.

## [1.7.2]

A feature-bearing patch for the 1.7 line. Rather than hold this work for the
larger 1.8 churn, the fixes and the smaller features that had already
stabilised on `main` are rolled into the stable line now: a rich preview
pane, persona/project settings on scheduled tasks, and a batch of streaming,
rendering, and nudge-delivery hardening.

> **⚠️ Before upgrading:** 1.7.2 adds Alembic migration `066`, applied
> automatically on first start. It adds two `Text NOT NULL DEFAULT ''`
> columns (`persona`, `project_id`) to the `scheduled_tasks` table; existing
> rows migrate to the empty default, which is byte-identical to pre-066
> dispatch behaviour. The change is additive and reversible, but — as always
> — back up your storage before upgrading (`pg_dump` for PostgreSQL; copy the
> database file for SQLite).

### Added

- **Rich preview pane + `open_preview` tool** — a workstream can now open a
  rendered preview (HTML, Markdown, and other kinds) in a pane beside the
  conversation via the new `open_preview` tool. Guarded fetches stream under
  a byte budget whose ceiling tracks the widest per-kind cap, preview blob
  ids are salted, and a preflight probe handles legacy charsets and a
  remote-assets opt-in. See `docs/tools.md`.
- **`allow_private_network` opt-in for `web_fetch` / `open_preview`** —
  private-address fetch and preview targets stay blocked by default; an
  operator can opt a workstream in through the settings registry when a
  private endpoint is genuinely intended. (Distinct from the 1.7.1 `[oidc]`
  flag of the same name, which governs identity-provider discovery.)
- **Persona + project settings on scheduled tasks** (migration `066`) — a
  scheduled task can now pin the **persona** and **project** of the
  workstream it dispatches, matching the levers a manually-created workstream
  already carries. Both default to empty (kind-default persona / no project),
  so existing schedules dispatch exactly as before.

### Fixed

- **Streaming fast-path overflow recovery** — fast-stream tokens are now
  batched and overflowed SSE listeners recover instead of stalling (and
  `connectSSE` no longer opens into a hidden background tab). The same
  overflow-recovery companions were carried to the coordinator pane, so a
  coordinator watching many children recovers dropped listeners the same way
  the live-session view does.
- **Renderer containment** — markdown sentinel-forgery and recursive-frame
  content loss are contained, and an indented fence close no longer drags its
  indent into the enclosed code content.
- **Idle nudge / wake delivery** — nudge and wake delivery is hardened across
  session eviction, cancellation, and identity rebinds; the wake gate now
  requires a real nudge queue, refused wakes are logged, and
  `initial_message_status` is typed as a closed enum on the wire.
- **`web_fetch` extraction inherits model settings** — the completion that
  extracts content from a fetched page now inherits the workstream's model
  settings instead of falling back to defaults.
- **UI panes** — ephemeral panes close on split-dismiss instead of orphaning
  a tab, and an unsplit skips the redundant refresh after an ephemeral pane
  closes.
- **Shared code-highlight CSS** — renderer-output CSS is shared so the console
  and coordinator panes highlight code identically.

### Security

- **`Content-Disposition` filenames made wire-safe** — download filenames
  derived from user-controlled text are sanitised (latin-1- and
  control-char-safe, quoting-safe) before they reach the `Content-Disposition`
  response header, including the fallback path.

### Documentation

- **HYPOTHESIS.md: daemons + the outer loop, plus a plain-language PRIMER** —
  the harness north-star document gains its daemon / outer-loop treatment and
  a new top-level `PRIMER.md`.

## [1.7.1]

A maintenance and hardening patch for the 1.7 line. No schema migrations;
the credential-redaction work below is additive and needs no configuration
change. The one new operator-facing knob is the opt-in `[oidc]
allow_private_network` flag (default off).

### Security

- **Credential redaction hardened across the tool-call surface** — the
  redactor that scrubs secrets from tool arguments and log previews was
  reworked on both the backend and the browser to close several leak paths
  and to fix false-positive and performance issues. Malformed tool-call
  arguments are now legalised before they reach the wire; the tool-args log
  preview scrubs credentials and control characters; and the coordinator's
  tool-call cards gain a matching client-side redaction pass so the JS and
  backend redactors stay at parity. Pattern coverage now includes
  `secret_access_key` / `aws_secret_access_key` multi-segment keys, bare
  `token=` / `key=` forms (guarded by a negative lookbehind to avoid
  false positives), and SQLAlchemy `+driver`-qualified connection-string
  schemes matched case-insensitively.
- **OIDC SSRF guard: `[oidc] allow_private_network` opt-in** — self-hosted
  identity providers on private networks can now be reached by setting
  `allow_private_network = true` under `[oidc]` (default off; the MCP OAuth
  path stays strict). Rejections of discovered endpoints carry the opt-in
  hint so the misconfiguration is self-explanatory. See `docs/oidc.md`.

### Added

- **Persona discoverability + forgiving name resolution** — personas are
  now discoverable by agents, and persona-name resolution tolerates
  case/whitespace variation; a not-found resolution reports the offending
  input verbatim instead of a bare error.

### Fixed

- **MCP transport lifecycles routed through per-entry owner tasks**
  (#787/#788) — static and pooled MCP transport lifecycles are now driven
  by per-server / per-entry owner tasks, with a hardened disarm-sweep loop
  guard and targeted exception handling in place of a broad `BaseException`
  arm, so a dying transport can no longer spin the CPU or strand delivery.
- **Client-construction failures surface as misconfiguration, not raw
  500s** — a model whose client cannot be constructed now reports a factory
  misconfiguration, and the raw exception text is kept out of the resulting
  503 response.
- **Postgres history search survives oversized rows** — a conversation row
  exceeding Postgres' full-text limits no longer aborts history search.
- **Agent-tool render is idempotent** — tool rendering no longer deep-copies
  a tool definition until a description actually changes, so no-persona
  sessions share the tool constant (correctness plus a hot-path allocation
  win).
- **Private-project workstream visibility scoped to members** — workstreams
  in a private project are visible to project members only, not to every
  admin; coordinator tenancy checks now use request-scoped storage.
- **Pane hotkeys work off macOS and match across surfaces** — the pane
  keyboard shortcuts no longer collide with browser accelerators on
  non-macOS platforms and behave consistently across surfaces.

## [1.7.0]

The headline of the 1.7 line is **Personas** — operator-authored control
over how each workstream composes its system message and capability
envelope. The rest of the release hardens the pieces a persona leans on:
concurrent approvals, cross-provider reasoning-effort control, cooperative
compaction, multi-user session safety, and MCP resilience for unattended
work.

> **⚠️ Before upgrading:** 1.7.0 adds Alembic migrations `062`–`065`,
> applied automatically on first start (projects, personas, and two
> smaller schema tidy-ups). Migration `063` creates the `personas` table
> with its six seed personas and converts existing `creative_mode`
> workstreams to the `writer` persona in place. The changes are additive
> to your conversation data, but — as always — back up your storage before
> upgrading (`pg_dump` for PostgreSQL; copy the database file for SQLite).

**Breaking changes at a glance** (details in the sections below): the
`/creative` REPL toggle removed (replaced by the `writer` persona), the
`turnstone-bootstrap` entry point renamed to `turnstone-doctor`, and the
approval-status API/SDK field `pending_approval_details` changed from a
single object to a list (one entry per concurrent approval cycle).

### Added

- **Personas** (#683) — a named, reusable bundle attached to a workstream
  at creation, controlling system-message composition and the capability
  envelope via exactly four levers: base-prompt override, tool visibility
  set, MCP on/off, and memory on/off. The persona is resolved once and
  snapshotted into `workstream_config`; editing or archiving a persona
  never changes an existing workstream. Six seed personas ship with
  migration `063` (`engineer` and `orchestrator` are the per-kind
  defaults with no overrides, so zero-touch behavior is unchanged;
  `scribe`, `researcher`, `writer`, and `executive` are curated
  envelopes). Selectable on every creation surface (web pickers, the
  create API/SDKs, coordinator `spawn_workstream` / `spawn_batch`, and
  `turnstone --persona <name>`); authored in the console's new
  Governance → Personas tab (`persona.{create,read,write}` perms,
  archive-only lifecycle). See `docs/personas.md`.
- **Projects — governed resource containers** (#724) — group workstreams
  and their resources under a project (migration `062`), with
  project-scoped memory, a per-project resources view, a project column on
  the saved list, and server-enforced private-project workstream
  visibility.
- **Task-agent sub-harness** (#732) — a spawned task agent now runs on its
  own Turn-IR sub-harness with parent-tagged step events: its sub-tool
  steps nest inside an expandable card in the parent trajectory, its
  sub-trajectory is recallable, and each agent gets read isolation from
  its siblings.
- **MCP static-server autonomous reconnect** (#768) — statically
  configured MCP servers are now kept live by a health loop
  (capped-jittered backoff, ping-based liveness) instead of silently
  staying dead after the first transport drop.
- **Attachments — capability-gated client-side fallback** — when the
  active model can't natively handle an attachment, the client degrades
  gracefully (PDF → extracted text, audio → transcript) instead of
  failing the turn.
- **Eval measurement / optimizer split** (#763, #765) — `turnstone-eval`
  is now a measure-only substrate with the prompt optimizer factored out,
  plus a new skill-adherence measurement mode.
- **Deployment examples** — a vLLM + LiteLLM unified-memory inference
  example showing a 3-model co-resident stack with an HF loader (#686,
  #688), and an Altair + `vl-convert-python` visualization stack (#685).
- **Concurrent approvals and a long-session frontend overhaul** (#754,
  #755, #773, #775) — the live-session frontend was reworked for long
  runs (the pipeline is wedge-proofed and its hot paths de-O(N)'d), and on
  top of it a workstream can now hold more than one tool call awaiting
  approval at a time. Each parallel batch gets its own approval cycle,
  with one card per pending call in the interactive and coordinator UIs,
  cycle-keyed tracking in Slack and Discord, and cycle-routed resolution
  across the server/console/SDK APIs; sub-agent tool gates run the
  intent-judge pipeline as their own generation. The send button no longer
  sticks disabled after a batch resolves — orphaned approval cycles are
  pruned and the app is the sole owner of the button state.
  *(BREAKING: the `pending_approval_details` field is now a list, oldest
  first.)*
- **Reasoning-effort control on every provider lane** (#771, #774) — the
  session effort knob now reaches local backends too: it drives
  `chat_template_kwargs` on the anthropic-compatible and openai-compatible
  lanes and threads through to Gemini and xAI, alongside the commercial
  providers that handle effort natively. The console surfaces each model's
  effective effort ladder in plain words and adds an always-on
  thinking-mode option to the model form. Effort snapping is ordinal —
  it rounds up and caps at the model's ceiling rather than silently
  dropping.

### Changed

- **Skills are capability-context, not identity** (#762) — a task agent's
  identity now comes from its persona; an applied skill's body is demoted
  to capability context and moved out of the identity system message.
  Skill-body substitution is unified across every invocation context so
  the same skill renders identically whether loaded interactively, by the
  model, or inside a sub-agent.
- **`turnstone-doctor` replaces `turnstone-bootstrap`** (#718)
  *(BREAKING)* — the setup/diagnostics entry point is renamed; update any
  scripts or service units that invoke `turnstone-bootstrap`.
- **Honest cancellation dispositions** — cancelled or timed-out
  side-effecting tools now report an `UNKNOWN` disposition rather than a
  flat failure, tool dispositions are typed (not just prose), and a
  coordinator cancel propagates down the sub-tree.
- **Multi-user shared-workstream context** (#750) — in a shared
  workstream, send is gated to the acting participant while a turn is in
  flight (both the interactive and coordinator surfaces), cross-user
  mid-turn interjections are blocked, and shared-workstream state plus
  fork sender attribution are now durable.
- **Cooperative compaction** (#730) — the context budget is anchored to
  the provider's true capacity, the summary call is chunked so it can't
  overflow, and the active plan and the outstanding ask are carried across
  compaction verbatim. The `recall` tool is scoped to the compacted-away
  past.
- **Intent judge sees the full tool arguments** (#760) — the judge's
  argument projection is no longer narrowed, so it stops issuing confident
  false denials on a partial view. The output-guard judge sources its real
  context window, and `context_window = 0` in `config.toml` now means
  auto-detect.

### Fixed

- **Compaction resume hardening** (#731) — checkpoint markers are
  persisted so resume rehydration is bounded, context-overflow on resume
  is recovered across providers, and a recognized rate-limit is no longer
  misclassified as context overflow.
- **MCP unattended-work resilience** (#706, #742, #767) — dead-transport
  handling is completed, consented OAuth (OBO) tokens are refreshed
  proactively so autonomous runs don't strand on an expired grant, the
  Entra ID on-behalf-of impersonation flow blockers are closed (migration
  `065` adds the OIDC `oid`), and OAuth refresh failures are classified so
  a transient blip never revokes consent nor a dead grant strands the
  user.
- **Memory writes** (#735) — save/update is a single atomic upsert, and
  writing a memory no longer recomposes the system prefix mid-session.

### Removed

- **`/creative` removed** *(BREAKING)* — subsumed by the Personas feature
  above: the REPL toggle (and its tab completion) is gone, and the
  `writer` seed persona replaces it — start a session with
  `turnstone --persona writer` or pick *Writer* in the web
  pickers. Unlike the old fork, the writer persona composes the full
  system message, so session context and mandatory prompt policies now
  apply to prose-only sessions too. The `creative_mode` key in
  `workstream_config` is no longer read or written. Migration `063`
  converts existing creative-mode workstreams to the `writer` persona
  automatically, so they resume as writing sessions rather than as
  legacy defaults.

### Security

- **High-risk skill activation is gated** (#762) — a model-initiated load
  of a `high`- or `critical`-risk skill is gated and fails closed when the
  backing storage is unavailable, so an untrusted turn can't silently
  pull in a dangerous capability.
- **Dependency security floors** — `cryptography` and `starlette` are
  pinned to security-fixed minimums.
- **CI publish hardening** — the vendored-JS dispatch path refuses fork
  PRs, and `workflow_run` publishing is gated to same-repo tag pushes, so
  a fork can't trigger a release build.

## [1.6.0]

The first stable release of the 1.6 line — and the first under Apache 2.0.

> **⚠️ Before upgrading from 1.5.x:** 1.6.0 changes the internal
> conversation storage schema (Alembic migration `060`, applied
> automatically on first start). The migration converts existing
> workstreams and attachments in place — **back up your storage before
> upgrading** (`pg_dump` for PostgreSQL; copy the database file for
> SQLite). Background: discussion
> [#631](https://github.com/turnstonelabs/turnstone/discussions/631).

**Breaking changes at a glance** (details in the sections below):
`web_search` backend overhaul (Tavily/DuckDuckGo removed, `topic` →
`category`), the `man` / `math` / `plan_agent` built-in tools and the
plan-review protocol removed, and the body-keyed `/v1/api/command`
endpoint replaced by path-keyed workstream verbs.

### License

- **Relicensed to Apache 2.0** — from BUSL-1.1, effective with this
  release (#546, contributor assent record in #548). Versions 1.5.x and
  earlier remain under BUSL-1.1 as shipped, and the `stable/1.5` branch
  keeps its original LICENSE. New `NOTICE` and
  `CONTRIBUTORS.md` files; `THIRD-PARTY-NOTICES` refreshed to match the
  bundled library versions.

### Added

- **Mid-conversation system messages** — advisories, watch results,
  skill hints, and operator interjections are now first-class
  `role=system` turns in the trajectory instead of ad-hoc reminder
  envelopes. Models with native mid-conversation system support receive
  them verbatim; for everything else they fold into a nonce-fenced
  wrapper. The one-shot `_reminders` side-channel is gone.
- **Self-hosted SearxNG web search** — the `web_search` backend for
  local/vLLM models is now a bundled [SearxNG](https://searxng.org)
  service (in both compose stacks; internal network only). Configure via
  `tools.searxng_url` / `tools.searxng_engines`. Commercial providers
  keep their native server-side search; the model can target a corpus by
  passing `category` (`general`, `news`, `it`, `science`). Operators
  exposing the bundled SearxNG publicly: see the AGPL-3.0 §13 note in
  [docs/docker.md](docs/docker.md).
- **Endpoint-backed reranking** — a reranker is now a per-model
  definition (Cohere/Jina-compatible wire: vLLM, TEI, llama.cpp, or a
  commercial endpoint), disabled by default. When configured it scores
  `web_search` results and the BM25 retrieval surfaces (deferred tools,
  skills, memory) behind a `tools.rerank_bm25` toggle with a relevance
  floor; a calibration CLI (and calibrate-on-detect) tunes the floor
  per model.
- **Proactive memory relevance** — injected memories are selected by
  BM25 + reranker against the recent user messages instead of recency
  alone, and first composition defers to the first user turn so fresh
  sessions select against a real query.
- **Smart Approvals** — opt-in (default off): high-confidence `approve`
  verdicts from the intent judge auto-approve the tool call instead of
  waiting for a human, with a confidence threshold and verdict
  bookkeeping designed so a denied or reset judge never auto-fires.
- **Early-painted tool calls** — committed tool calls render immediately
  as pending cards (both UIs upgrade the card in place by `call_id`)
  instead of waiting for the judge verdict, so big parallel batches no
  longer sit invisible during judging.
- **Voice I/O v1** — speech-to-text and text-to-speech as model roles
  speaking the OpenAI audio wire protocol (#618); the interactive
  composer grows a mic button.
- **Rewind / retry / edit-first-message** — full UX in both the
  interactive UI and the coordinator pane, backed by shared path-keyed
  verb handlers (#549).
- **Workstream export** — download a conversation as OpenAI-format
  messages JSON.
- **Skills platform round** — `SKILL.md` ingestion learns
  `when_to_use` / `model` / `effort` / `paths`; prompt substitution
  supports `$ARGUMENTS`, `$N`, `$<name>`, and `${CLAUDE_*}` (#572);
  per-skill `disable-model-invocation` and `user-invocable` flags
  (#571); `skill` + `list_skills` unify into one dual-kind tool; new
  `model.skills.write` permission.
- **Coordinator hardening for small models** — workstream references in
  coordinator tool calls are validated with did-you-mean recovery, and
  `wait_for_workstream` fails fast with uniform `not_found` entries
  instead of hanging on a hallucinated `ws_id`.
- **Provider support** — Claude Fable 5 and Claude Opus 4.8; xAI/Grok
  via the OpenAI Responses lane; vLLM reasoning-field replay completes
  the reasoning-persistence work (#537).
- **Cluster-by-default deployment** — the compose stack fronts
  everything with Caddy and supports bare-metal node join; a one-line
  `curl | bash` installer bootstraps a node; nodes with no configured
  models boot into a degraded state instead of crash-looping; channel
  gateways stand by when no adapter token is set.
- **MCP OAuth tokens encrypted at rest**.
- **`turnstone-admin` reads `config.toml`** — same `[database]` section
  and precedence as the server (`CLI / config.toml > TURNSTONE_DB_* env
  > defaults`), including `pool_size` and the `ssl*` knobs it previously
  dropped; new `--config PATH` flag.

### Changed

- **Conversation storage and the provider wire are rebuilt around a
  canonical trajectory** (migration `060` — see the upgrade note).
  Internally a conversation is now a provider-neutral `Turn` sequence
  lowered to each provider's wire format at send time; provider-specific
  tool-call metadata rides an opaque producer-tagged lane (replayed
  verbatim to the producing provider, rebuilt for others); attachments
  become content-addressed, reference-counted rows resolved at the
  provider boundary; orphan tool-call repair happens once, at send time.
  Wire-visible behavior is unchanged for OpenAI-compatible providers;
  histories are preserved across the migration.
- **The console and web UI share one L-shell** — a left glyph rail, a
  tab bar, and a pane host now frame interactive chats, coordinator
  sessions, dashboards, and the admin panel as tabs in a single window;
  the standalone web UI adopts the same shell and the old split-pane
  layout is retired. Coordinator and interactive conversations render
  through shared `.conv-*` card builders, the rail collapses to a glyph
  strip (remembered per browser), mobile gets an off-canvas drawer, and
  the frontend is now ES modules end to end.
- **Admin panel modals → the Service Hatch shelf** — all ~35 admin
  modals are replaced by pane-scoped shelves plus a small dialog tier
  for confirmations. Schedules gain a cron builder with a next-3-runs
  preview endpoint, model capabilities render as an LED tile matrix, and
  the legacy modal machinery is deleted.
- **SSE delivery is resumable end to end** — per-workstream ring buffer
  with `Last-Event-ID` replay (cap raised 2,000 → 50,000), fresh-connect
  and reconnect unified on one event-id cursor (in-flight tool batches
  included), persisted `last_error` replays on connect, the console
  proxy forwards `Last-Event-ID`, and panes close their connections on
  `beforeunload` to stop multi-pane refresh from exhausting the
  browser's per-host connection cap (#539).
- **Workstream verbs are path-keyed** *(BREAKING)* — `rewind` / `retry`
  / `edit-first-message` live at
  `/v1/api/workstreams/{ws_id}/<verb>` alongside the other session
  verbs; the body-keyed `/v1/api/command` endpoint is removed (#549).
- **`/history` is projected server-side** — both UIs consume the same
  REST-first wire shape instead of re-deriving it client-side.
- **Saved workstreams & coordinators: card grid → sortable table** with
  model/skill/context columns, pagination, and a unified selector across
  both dashboards.
- **`tools.web_search_backend` accepted values** *(BREAKING)* — now `""`
  (auto), `"searxng"`, or `"mcp:server:tool"`. The old `"tavily"` and
  `"ddg"` values are gone; a config still set to either disables web
  search and logs a warning. Auto-detect resolves to SearxNG when
  `searxng_url` is set.
- **`web_search` tool: `topic` → `category`** *(BREAKING)* — renamed
  LLM-facing parameter; values map to SearxNG categories. The Tavily-era
  `finance` topic is gone.
- **Core install includes what most deployments use** — `anthropic`,
  `postgres`, `console`, and `tls` are core dependencies rather than
  extras.
- **NODES table → bottom-bar node picker** in the console.

### Fixed

- **Cluster mTLS actually survives operations** — certificate identity
  keys on the advertised host rather than the container ID, renewals are
  scoped per node, reloaded certs hot-swap into the live SSL context,
  and healthchecks/boot retries are mTLS-aware.
- **Intent-verdict lifecycle** — history replay ships risk-none verdict
  rows (live/replay parity), late verdicts persist as `superseded` for
  the audit trail instead of vanishing, bulk verdict insert tolerates
  per-row conflicts, and cancel-on-approval honors its run-to-completion
  contract.
- **Usage accounting** — dashboard totals were under-counting; auxiliary
  LLM spend (judge, rerank, memory) is now recorded.
- **Concurrent first-boot migrations** no longer deadlock on the
  advisory lock.
- **Output renderer** — single-`$` inline math no longer false-positives
  in prose; `strip_html` preserves block structure and drops a ReDoS
  risk.
- **Model registry** orders versions numerically (no more `1.10 < 1.9`
  selection).

### Removed

- **Tavily and DuckDuckGo `web_search` backends** *(BREAKING)* —
  replaced by the bundled SearxNG service. Removed:
  `tools.tavily_api_key`, `$TAVILY_API_KEY`, `[api].tavily_key`, and the
  `ddg` install extra. Point `TURNSTONE_SEARXNG_URL` at an existing
  instance or use the bundled one; no database migration required.
- **`man`, `math`, and `plan_agent` built-in tools** *(BREAKING)* —
  `man`/`math` duplicated `bash`; planning is better expressed as a
  `task_agent` running a planning skill. Also removed: the `math`
  sandbox executor, the read-only `AGENT_TOOLS` sub-agent set, the
  plan-review protocol (`/v1/api/plan`, `plan_review`/`plan_resolved`
  SSE events, `on_plan_review` hooks), and the `model.plan_*` settings.
  Interactive built-in tool count: 19 → 16.
- **`stable/1.4` track retired** — the maintenance policy is now the
  current stable plus one prior (`stable/1.6` + `stable/1.5` as of this
  release). 1.4's final release was `v1.4.0`; its tags and released
  artifacts remain available, under BUSL-1.1 as shipped.

### Security

- **Zero direct-HTML frontend** — every `innerHTML` sink across the
  console and web UI is replaced with DOM construction or `setSafeHtml`,
  inline handlers became delegated bindings, and CI lints pin the
  invariant (plus `var`-free and const-reassign checks) across all
  swept bundles.
- **Output guard grows an LLM stage** — merged with the heuristics as
  escalate-only (an LLM verdict can raise but never lower a heuristic
  positive), with annotated findings, a capability gate, and hardening
  against domain-camouflaged injection (#560, #573).
- **One trust-fence primitive** — operator and judge envelopes share a
  nonce-fenced wrapper (64-bit nonces, host-escaping); the output guard
  flags nonce forgery, and skill hints no longer echo model-controlled
  filter values into trusted text.
- **RBAC** — built-in role overrides get an editor, and several
  under-enforced permission gates are tightened (#585).
- **Permissive `config.toml` warns** — a single startup warning when the
  resolved config file is group- or world-readable; operators usually
  want `0600`.
- **Dependency floors** — `starlette>=1.0.1` (PYSEC-2026-161 host-header
  path injection) and `aiohttp>=3.14.0` (security release).

## [1.5.17]

Backports a clutch of coordinator-tool clarity fixes plus a watch-delivery
correctness fix from `main` to the `stable/1.5` track, plus a previously-
latent intent-verdicts persistence bug exposed by the new heuristic-verdict
INSERT paths.  No schema changes.

### Fixed

- **`intent_verdicts` PK collisions on every llm_fallback delivery** —
  async LLM-tier "llm_fallback" verdicts (`turnstone/core/judge.py` —
  `_deliver_fallbacks` and the in-loop fallback path) deliberately
  reuse the heuristic verdict's `verdict_id` so the row gets
  "upgraded in place" from `tier="heuristic"` → `tier="llm_fallback"`
  when the LLM judge times out, is cancelled, or returns no content.
  The consumer `_persist_intent_verdict` was doing a plain INSERT,
  hitting the `intent_verdicts_pkey` constraint on every fallback
  delivery; Postgres logged the duplicate-key error, the application
  try/except swallowed it at `log.debug`, and the row never actually
  got upgraded — the LLM judge's annotation
  (`"(LLM judge did not return a verdict)"`) was lost.  The collision
  rate exploded on this release because the new heuristic-INSERT
  paths in the auto-approve early-return branches of `approve_tools`
  (introduced below) leave no gap for the fallback to land cleanly
  into.  Fix: new `upsert_intent_verdict` storage method using
  `ON CONFLICT (verdict_id) DO UPDATE` that updates only `tier`,
  `reasoning`, `judge_model` — the three fields that genuinely
  change between heuristic and llm_fallback.  Every other column
  (identity, carried-verbatim, and `user_decision`) is excluded;
  `user_decision` in particular would otherwise be clobbered back
  to `"pending"` when a fallback arrives after the operator has
  already resolved the approval.  The bulk-INSERT path stays as
  plain INSERT — fresh UUIDs in `judge.evaluate` make in-turn dups
  impossible; the inverse race (fallback wins before bulk lands) is
  reachable but unchanged in observable behavior by this fix,
  documented at the bulk site for a future hardening pass.
- **Coordinator LLM re-spawn loops on large fan-outs** — the spawn-tool
  return JSON used `ws_id` as its key, which primed the model's recency
  bias to feed the spawn result straight back into another
  `spawn_workstream(ws_id=...)` call instead of progressing to
  `wait_for_workstream(ws_ids=[...])`.  On 10+ child fan-outs this cascaded
  into self-inflicted re-spawn loops.  The LLM-facing tool result now emits
  `child_ws_id` (the storage column / HTTP API contract is unchanged); the
  field name is already an existing project term so the rename aligns
  rather than introduces new vocabulary.  Also handles the silent
  upstream-omits-ws_id success-shape edge that previously emitted
  `{"child_ws_id": null}` to the LLM — now surfaces a tool error so the
  model retries rather than chasing a null id.
- **`inspect_workstream` blowing the coordinator context budget** — a
  coord doing a fan-out wave against tool-heavy children could land
  >100 KB of raw output per inspect call, and the previous safety net
  (`_truncate_output`'s head+tail strategy) silently dropped *middle*
  messages — exactly the wrong shape for understanding a child's
  trajectory (the FIRST sets the brief, the LAST shows the conclusion,
  the middle is the connective tissue).  Output now goes through a
  three-tier degradation ladder mirroring the search tool's
  `_format_search_results`: `_tier="full"` (every message verbatim) →
  `_tier="compact"` (per-message head/tail-snipped content + snipped
  `tool_calls.arguments`, falling through a `(20,30)` / `(10,20)` /
  `(5,10)` message-list trim ladder) → `_tier="skeleton"` (counts, role
  distribution, last-assistant preview).  Budget 32 KiB matches the
  search tool's; the chosen tier is annotated on the response so the
  model can recall with a tighter `message_limit` if signal was lost.
- **Auto-approved verdicts indistinguishable from pending review** —
  `intent_verdict` rows for auto-approved tool calls landed with
  `user_decision=""`, which read identically to "still waiting for the
  operator" in the audit trail and led to a real misdiagnosis incident.
  The column now carries an explicit vocabulary at insert: `pending` /
  `approved` / `denied` / `timeout` / `policy` / `blanket` / `skill` /
  `always` / `auto_approve_tools`.  The auto-approve early-return
  branches in `approve_tools` now persist heuristic verdicts stamped
  with their reason (previously dropped on the floor), and late LLM-tier
  verdicts that arrive for an already-auto-approved call_id are stamped
  via a TTL-pruned lookup map — so the audit row carries the
  auto-approve reason even when the LLM judge daemon completes after
  the synchronous approval cycle finished.  `resolve_approval` gains a
  `timeout` kwarg writing `"timeout"` (the previous shape collapsed
  passive timeouts and active denials into the same column).
- **`list_skills` empty `allowed_tools` misread as "no tool access"** —
  the response previously emitted `"allowed_tools": []` for every skill
  that hadn't declared an auto-approve allowlist, which a coordinator
  model read as "this skill can't use any tools" (real misdiagnosis: a
  code-review child appeared to have been spawned with zero tool
  access).  The field is now omitted entirely when empty — absence
  carries the unambiguous meaning "no tool is pre-approved for this
  skill", presence (non-empty list) keeps the standard Claude Code
  skill-spec shape.  The tool description rewrite makes the
  auto-approve-allowlist semantics explicit so a future reader doesn't
  re-derive the gating misread.
- **Watch terminal-fires silently dropped on backpressure** —
  delivery now routes terminal events through the same path as
  normal fires instead of being filtered out when the consumer was
  saturated.

### Documentation

- **Storage `LIKE_ESCAPE` contract** — clarify that callers passing
  `.like(escape=...)` must use the same escape character that the
  storage helper assumes; previous wording let a reader pass a
  different escape and silently produce no matches.

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
