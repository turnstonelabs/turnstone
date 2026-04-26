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
