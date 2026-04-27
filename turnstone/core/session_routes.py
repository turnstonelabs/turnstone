"""Shared HTTP route registrar for workstream-shaped sessions.

Both node and console processes mount the workstream HTTP tree at
``/v1/api/workstreams/`` via this registrar against their own
:class:`~turnstone.core.session_manager.SessionManager` (interactive
on the node, coordinator on the console). One URL shape, two
processes, kind-specific policy in :class:`SessionEndpointConfig`
captured by closure when the handler factory is called at app
construction.

Three registrar functions:

- :func:`register_session_routes` — verbs both kinds expose
  (``new``, ``close``, ``open``, ``delete``, ``send``, ``approve``,
  ``cancel``, ``events``, ``history``, ``detail``, ...).
  All handlers in :class:`SharedSessionVerbHandlers` are optional;
  ``None`` skips the route, so one bundle describes either kind.
- :func:`register_coord_verbs` — coord-only verbs (``trust``,
  ``restrict``, ``stop_cascade``, ``close_all_children``,
  ``children``, ``tasks``, ``metrics``) that read or mutate state
  that doesn't exist on interactive workstreams.

Some verbs in :class:`SharedSessionVerbHandlers` ship as factory-
returned closures (e.g. :func:`make_approve_handler`,
:func:`make_close_handler`) that bake their
:class:`SessionEndpointConfig` (and any verb-specific args like
``audit_emit``) in at app-construction time. Both node and console
call the factory during startup and pass the result as
``handlers.approve`` / ``handlers.close``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from starlette.responses import JSONResponse
from starlette.routing import Route

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from turnstone.core.session_manager import SessionManager
    from turnstone.core.workstream import Workstream, WorkstreamKind

log = get_logger(__name__)


Handler = Callable[["Request"], Awaitable["Response"]]
PermissionGate = Callable[["Request"], "JSONResponse | None"]
ManagerLookup = Callable[["Request"], tuple["SessionManager | None", "JSONResponse | None"]]
TenantCheck = Callable[
    ["Request", str, "SessionManager"],
    "JSONResponse | None",
]
# (request, ws_id, mgr) -> (owner_user_id, error_response). Owner is
# the user_id attachments are filed under; error is a 404 when the ws
# doesn't exist anywhere (memory or storage).
AttachmentOwnerResolver = Callable[
    ["Request", str, "SessionManager"],
    tuple[str, "JSONResponse | None"],
]
# (request, ui) — kind's spawn-time bookkeeping. Interactive bumps
# ``_metrics.record_message_sent`` + per-UI message counters; coord
# has no analog and wires ``None``.
SpawnMetricsHook = Callable[["Request", Any], None]


class CancelForensics(Protocol):
    """Pure-read snapshot the lifted ``cancel`` body surfaces as ``dropped``.

    Returns a dict with whatever in-flight session / UI state the
    kind wants to expose to the caller (pending-approval tool names,
    queued-message count + preview, etc.). Kinds that don't need a
    forensic snapshot wire ``None`` on the cfg and the lifted body
    returns an empty ``dropped`` dict in the response.

    Protocol-typed (rather than ``Callable``) because the keyword-only
    ``was_running`` argument the lifted body passes can't be expressed
    by a plain ``Callable`` type alias.
    """

    def __call__(self, session: Any, ui: Any, *, was_running: bool) -> dict[str, Any]:
        """Return the ``dropped`` snapshot for the cancel response."""


# (alias_or_id) -> canonical_id_or_None. Interactive's lifted
# ``open`` body (:func:`make_open_handler`) pre-resolves user-friendly
# aliases to the canonical hex ws_id via
# :func:`turnstone.core.memory.resolve_workstream` so callers can
# pass either shape. Coord wires ``None`` (coord workstreams are
# addressed by hex id only).
AliasResolver = Callable[[str], str | None]
# (request, ws) -> None. Optional kind-specific post-load callback
# the lifted ``open`` body fires after the workstream is loaded into
# the manager. Interactive uses it to push a ``clear_ui`` + history
# replay onto the UI listener queue and to enqueue a handler-side
# ``ws_created`` event onto the global SSE queue (the global-queue
# emission stays out of band on interactive — see
# :class:`SessionKindAdapter` docstring for the asymmetry rationale).
# Coord wires ``None`` and relies on the cluster collector fan-out
# triggered by ``CoordinatorAdapter.emit_rehydrated``.
OpenPostLoad = Callable[["Request", "Workstream"], None]
# (request, ws) -> None. Optional audit emitter for the ``open``
# event. Same shape as ``CloseAuditEmitter``'s leading args. Coord
# wires ``None`` (coord doesn't audit open today).
OpenAuditEmitter = Callable[["Request", "Workstream"], None]


class EventsReplay(Protocol):
    """Pure-read generator of the per-kind initial SSE replay payload.

    The lifted ``events`` body calls this once per SSE connection
    *after* the per-UI listener queue is registered, but *before* the
    live event loop starts. Each yielded dict gets JSON-serialised
    and sent as a single ``data:`` line to the client.

    Interactive yields five things on connect: ``connected`` (model +
    skip_permissions), ``status`` (token usage + context %, only when
    ``session._last_usage`` exists), ``history`` (replayed conversation),
    ``pending_approval`` + cached intent verdicts, and
    ``pending_plan_review``. Coord yields just two: ``pending_approval``
    and ``pending_plan_review`` (the rest aren't needed because coord's
    dashboard fetches history via a separate ``/history`` endpoint
    and doesn't render the per-tab status bar). Kinds that don't
    need any pre-replay wire ``None`` and the live loop starts
    immediately.
    """

    def __call__(self, ws: Workstream, ui: Any, request: Request) -> Iterable[dict[str, Any]]:
        """Return the iterable of initial replay events."""


# (request) -> Executor for the SSE live-loop's blocking `queue.get`
# wait. Interactive returns the dedicated ``sse_executor``
# (200-thread pool created at lifespan setup) so the SSE poll path
# stays isolated from every other ``asyncio.to_thread`` caller in
# the process (storage, router, audit). Coord returns ``None`` and
# the lifted body falls through to ``asyncio.to_thread`` (default
# executor, capped at ``min(32, os.cpu_count() + 4)`` workers) —
# coord's per-process SSE concurrency stays well under that ceiling
# and adding a dedicated pool would over-engineer for the LOC win.
SseExecutorLookup = Callable[["Request"], Any]


# (request, body, uid, uploaded_files) -> JSONResponse | None.
# Optional kind-specific gate the lifted ``create`` body fires after
# body parsing + uid resolution but before skill resolution and
# ``mgr.create``. Returns ``None`` to continue, or a 4xx response
# to short-circuit. Interactive wires gates for ws_id format,
# kind=INTERACTIVE, parent_ws_id ownership, attachments+resume_ws
# combo. Coord wires a 401-on-empty-uid (admin tokens always carry a
# uid in practice; the gate is defensive). Mostly read-only — the
# parent_ws_id ownership gate does a single storage lookup but
# doesn't mutate anything.
CreateRequestValidator = Callable[
    ["Request", dict[str, Any], str, list[tuple[str, str, bytes]]],
    "Awaitable[JSONResponse | None]",
]
# (request, body, uid, skill_data, skill_id, applied_skill_version) -> kwargs.
# Builds the kwargs dict for ``mgr.create``. Both kinds call
# ``mgr.create`` with the same callable shape, but the kwargs they
# pass differ (interactive threads model + judge_model + client_type +
# parent_ws_id + ws_id; coord threads only the smaller subset).
# Captured in a per-kind callable rather than a flag-soup so the
# kwargs dict construction stays readable at the wire-up site.
CreateKwargsBuilder = Callable[
    ["Request", dict[str, Any], str, dict[str, Any] | None, str, int],
    dict[str, Any],
]
# (request, ws, body, uid, skill_data, applied_skill_version, attachment_ids) ->
# extra response fields. Kind-specific tail end the lifted ``create``
# body fires after the workstream is built, attachments are saved,
# and audit is emitted. Returns extra fields to merge into the
# response (e.g. interactive returns ``{resumed, message_count}``;
# coord returns ``{}``). May spawn worker threads / register watch
# runners / persist skill session config / dispatch initial messages
# / pin routing. The factory does NOT wrap the call in try/except:
# post-install failures should surface to the caller as 5xx so the
# operator sees the misconfig instead of a half-built workstream.
CreatePostInstall = Callable[
    [
        "Request",
        "Workstream",
        dict[str, Any],
        str,
        dict[str, Any] | None,
        int,
        list[str],
    ],
    "Awaitable[dict[str, Any]]",
]
# (request, ws, body, uid) -> None. Audit emitter for the create
# event. Interactive emits ``workstream.created`` with
# ``{kind, parent_ws_id}`` detail; coord emits ``coordinator.create``
# with ``{coord_ws_id, src, name}`` detail. Wrapped in try/except by
# the factory — audit-write failures shouldn't surface as HTTP 500
# (mirrors the close / cancel / open lift contracts).
CreateAuditEmitter = Callable[
    ["Request", "Workstream", dict[str, Any], str],
    None,
]


# (ws_ids) -> {ws_id: title-or-None} bulk lookup. Interactive wires
# :func:`turnstone.core.memory.get_workstream_display_names` so the
# active-list endpoint resolves every alias in one storage round-trip
# instead of the pre-lift N+1 (one SELECT per row). Coord wires
# ``None`` (coord doesn't have an alias surface today) and the lifted
# body uses ``ws.name`` directly. Returns a dict keyed on every
# requested ws_id; missing rows map to ``None``, and the caller
# falls back to ``ws.name`` per-row.
ListResolveTitles = Callable[[list[str]], dict[str, str | None]]
# (request) -> set of ws_ids currently held in memory by the kind's
# manager. Coord wires a callable that returns
# ``{ws.id for ws in coord_mgr.list_all()}`` so the saved-card list
# can defence-in-depth filter out coordinators currently in the warm
# pool (a coord can be ``state='closed'`` on disk briefly while the
# close-emit sequence races the in-memory pop). Interactive wires
# ``None``: an interactive workstream that's both saved and loaded
# is a normal display state, not a race the saved card needs to
# hide. Async because the coord-side implementation runs through
# ``asyncio.to_thread`` (the manager lock is acquired in
# ``coord_mgr.list_all``).
SavedLoadedLookup = Callable[["Request"], Awaitable[set[str]]]


@dataclass(frozen=True)
class AttachmentUploadHelpers:
    """Process-local hooks the lifted attachment factories call into.

    The classification + per-(ws,user) lock are stateful concerns that
    don't belong on the (frozen) :class:`SessionEndpointConfig`
    directly: ``sniff_image_mime`` and ``classify_text_attachment``
    are pure but defined in the kind's owning module;
    ``upload_lock`` returns a process-local cached lock. Bundling
    them on a separate dataclass keeps the cfg declarative and lets
    callers share one helper instance across kinds if the policies
    converge later.
    """

    sniff_image_mime: Callable[[bytes], str | None]
    classify_text_attachment: Callable[
        [str, str, bytes],
        tuple[str | None, str | None],
    ]
    upload_lock: Callable[[str, str], Any]


@dataclass(frozen=True)
class SessionEndpointConfig:
    """Per-kind policy the lifted handler bodies consult at request time.

    Instantiated once per process during app construction and passed
    to the verb factory (e.g. :func:`make_approve_handler`,
    :func:`make_close_handler`, :func:`make_send_handler`), which
    captures it via closure. The request-time handler reads ``cfg``
    from the closure rather than ``app.state`` so the dependency is
    visible at the wire-up site.

    - ``permission_gate``: kind's pre-handler permission check
      (e.g. ``admin.coordinator`` for coord, ``None`` for interactive
      which has no per-handler scope check beyond auth middleware).
      Returns the rejection response when the gate fails, ``None``
      when the request passes.
    - ``manager_lookup``: returns ``(SessionManager, None)`` when the
      kind's manager is loaded, or ``(None, JSONResponse)`` with a
      503 when the subsystem isn't available (coord on a console
      without configured models). For interactive the lookup just
      returns ``(app.state.workstreams, None)``.
    - ``tenant_check``: per-``ws_id`` existence + access gate.
      Interactive wires :func:`_require_ws_access` (which 404s when
      the workstream doesn't exist; row-level ownership is NOT
      enforced — turnstone is a trusted-team tool, ``admin.workstreams``
      scope is the cluster-wide gate). Coord sets this to ``None``
      and relies on ``admin.coordinator`` from ``permission_gate``
      plus an in-memory ``coord_mgr`` lookup at handler time.
    - ``not_found_label``: the message body for the 404 returned when
      the manager has no such ws_id ("Workstream not found" for
      interactive; "coordinator not found" for coord).
    - ``audit_action_prefix``: the dot-namespaced prefix the kind
      uses for its audit actions ("workstream" → ``workstream.cancel``;
      "coordinator" → ``coordinator.cancel``).

    Capability flags (added with the P1.5 ``send`` body lift):

    - ``supports_attachments``: when ``True``, the lifted ``send``
      handler resolves attachment_ids, reserves under a send_id token,
      and threads them through ``ChatSession.send`` /
      ``ChatSession.queue_message``. Both kinds wire ``True`` post-P1.5
      (the storage layer was always kind-agnostic; the gate stays
      around so a kind that hasn't lit up its UI surface yet can
      defer the verb body changes).
    - ``attachment_owner_resolver``: resolves the ``user_id`` to scope
      attachments under for a given request + ws_id. Required when
      ``supports_attachments`` is ``True``.
    - ``spawn_metrics``: optional bookkeeping hook fired once per
      ``send`` that spawns a fresh worker (queue-reuse path skips it).
      Interactive wires its WebUI per-conversation counters; coord
      wires ``None``.
    - ``emit_message_queued``: when ``True`` and the dispatcher takes
      the live-worker enqueue path, the lifted body emits a
      ``message_queued`` event onto the workstream's listener queue
      via ``ui._enqueue``. Both kinds wire ``True`` since both UIs
      have a listener queue.
    """

    permission_gate: PermissionGate | None
    manager_lookup: ManagerLookup
    tenant_check: TenantCheck | None
    not_found_label: str
    audit_action_prefix: str
    supports_attachments: bool = False
    attachment_owner_resolver: AttachmentOwnerResolver | None = None
    attachment_helpers: AttachmentUploadHelpers | None = None
    spawn_metrics: SpawnMetricsHook | None = None
    emit_message_queued: bool = True
    # (session, ui, *, was_running) -> dict. When set, the lifted
    # ``cancel`` body calls this and surfaces the result as the
    # ``dropped`` key on the response. Interactive wires
    # ``_capture_cancel_forensics`` so the model-invoked
    # ``cancel_workstream`` tool can tell operators what got killed
    # (pending-approval tool names, queued-message count + preview).
    # Coord wires ``None`` — no forensic surface today; the lifted
    # body still returns ``dropped: {}`` for response-shape parity.
    cancel_forensics: CancelForensics | None = None
    # (alias_or_id) -> canonical_id_or_None. When set, the lifted
    # ``open`` body resolves the path-param ws_id through this
    # callable before any storage lookup. Interactive wires
    # :func:`turnstone.core.memory.resolve_workstream` so user-friendly
    # aliases ("my-debug-ws") map to canonical hex ids. Coord wires
    # ``None`` — coord uses hex ids only.
    open_resolve_alias: AliasResolver | None = None
    # (request, ws) -> None. Kind-specific post-load callback fired
    # by the lifted ``open`` body after ``mgr.open(ws_id)`` returns
    # the workstream. Interactive uses it to send the UI-replay
    # events (``clear_ui`` + history) and to enqueue a handler-side
    # ``ws_created`` onto the global SSE queue (out-of-band path —
    # see :class:`SessionKindAdapter` docstring for why interactive's
    # creation events stay outside the manager's emit_*). Coord
    # wires ``None`` and lets the cluster collector handle the
    # transition via ``CoordinatorAdapter.emit_rehydrated``.
    open_post_load: OpenPostLoad | None = None
    # (ws, ui, request) -> Iterable[dict]. Kind-specific initial
    # SSE replay payload the lifted ``events`` body yields after
    # registering the per-UI listener queue but before the live
    # event loop. Interactive replays connected + status + history
    # + pending_approval (with cached intent verdicts) +
    # pending_plan_review. Coord replays just pending_approval +
    # pending_plan_review (its dashboard fetches history via a
    # separate ``/history`` endpoint and doesn't render the per-tab
    # status bar). Kinds that don't need pre-replay wire ``None``.
    events_replay: EventsReplay | None = None
    # (request) -> Executor for the SSE live-loop's blocking
    # ``queue.get`` wait. Interactive returns the dedicated
    # ``request.app.state.sse_executor`` (200-thread pool) so SSE
    # polling stays isolated from every other ``asyncio.to_thread``
    # caller in the process; coord wires ``None`` and the lifted
    # body falls through to the default executor. See
    # :data:`SseExecutorLookup` docstring above.
    sse_executor_lookup: SseExecutorLookup | None = None
    # When ``True``, the lifted ``create`` body parses
    # ``multipart/form-data`` (with one ``meta`` JSON field + zero or
    # more ``file`` parts) in addition to plain ``application/json``.
    # Both kinds wire ``True`` post-create-lift — coord gains
    # create-time attachments here (§ Post-P3 reckoning item #1).
    # The actual attachment validation+save+rollback always uses the
    # storage layer (kind-agnostic since P1.5); this flag only
    # toggles whether the multipart parse is attempted at all.
    create_supports_attachments: bool = False
    # When ``True``, the lifted ``create`` body honours a ``user_id``
    # field in the request body if the caller's auth token comes from
    # a trusted service (currently just ``"console"``). Interactive
    # wires ``True`` so console-proxied creates can carry the real
    # end user's identity through to the workstream owner. Coord
    # wires ``False`` — coord create runs only on the console process
    # and the operator's auth result is the source of truth.
    create_supports_user_id_override: bool = False
    # (request, body, uid, uploaded_files) -> JSONResponse | None.
    # Per-kind pre-create gate (ws_id format, parent ownership, kind
    # validation, etc. on interactive; 401-on-empty-uid on coord).
    # ``None`` skips the gate entirely.
    create_validate_request: CreateRequestValidator | None = None
    # (request, body, uid, skill_data, skill_id, applied_skill_version)
    # -> kwargs for ``mgr.create``. Required when the kind mounts a
    # ``create`` handler — the lifted body has no opinion on the
    # kind-specific kwarg shape and threads whatever this returns
    # straight through to ``await asyncio.to_thread(mgr.create, **kwargs)``.
    create_build_kwargs: CreateKwargsBuilder | None = None
    # (request, ws, body, uid, skill_data, applied_skill_version,
    # attachment_ids) -> extra response fields. Kind-specific tail
    # end fired after attachments save + audit. Interactive returns
    # ``{resumed, message_count}`` and spawns the initial-message
    # worker thread; coord returns ``{}`` and dispatches via
    # ``coord_adapter.send`` when an initial_message is provided.
    # ``None`` skips the post-install entirely (response is just
    # ``{ws_id, name, ...}`` with empty parity fields).
    create_post_install: CreatePostInstall | None = None
    # (ws_ids) -> {ws_id: title-or-None} — bulk title lookup for the
    # active-list endpoint. Interactive wires
    # ``get_workstream_display_names`` so every row's alias resolves
    # in one storage round-trip; coord wires ``None`` (no alias
    # surface today). See :data:`ListResolveTitles`.
    list_resolve_titles: ListResolveTitles | None = None
    # Kind classifier for the lifted ``list``/``saved`` factories'
    # storage filter. Required when a kind mounts either handler —
    # the factories pass it straight through to
    # ``list_workstreams_with_history(kind=...)``. Distinct from
    # ``audit_action_prefix`` (audit-action namespacing) so adding a
    # third kind doesn't have to overload the audit prefix as a
    # filter. ``None`` is allowed for kinds that don't mount a
    # list/saved handler.
    list_kind: WorkstreamKind | None = None
    # Storage-side state filter for the saved-list endpoint. Interactive
    # wires ``None`` — saved sidebar shows every persisted interactive
    # workstream regardless of state (the storage layer already
    # excludes ``state='deleted'`` tombstones). Coord wires
    # ``"closed"`` so only explicitly-closed coordinators surface in
    # the saved-card grid; active / in-flight rows live in the active
    # list.
    saved_state_filter: str | None = None
    # (request) -> set of ws_ids in the kind's in-memory pool. Coord
    # wires a coroutine that returns ``{ws.id for ws in
    # coord_mgr.list_all()}`` (defence-in-depth filter — see
    # :data:`SavedLoadedLookup`). Interactive wires ``None``.
    saved_loaded_lookup: SavedLoadedLookup | None = None


@dataclass(frozen=True)
class AttachmentHandlers:
    """The four-handler quartet for the per-workstream attachment surface.

    Grouped so the type system enforces that you can't mount
    upload-without-delete or list-without-content (a half-mounted
    surface leaves broken frontend flows). Set
    :attr:`SharedSessionVerbHandlers.attachments` to ``None`` for
    kinds that don't expose attachments yet.
    """

    upload: Handler  # POST   {prefix}/{ws_id}/attachments
    list: Handler  # GET    {prefix}/{ws_id}/attachments
    get_content: Handler  # GET    {prefix}/{ws_id}/attachments/{attachment_id}/content
    delete: Handler  # DELETE {prefix}/{ws_id}/attachments/{attachment_id}


@dataclass(frozen=True)
class SharedSessionVerbHandlers:
    """Bundle of HTTP handler callables for verbs both kinds expose.

    All handlers are optional; ``None`` skips that route. One bundle
    describes either kind — interactive omits the per-``{ws_id}``
    interaction verbs (``send`` / ``approve`` / ``plan`` / ``cancel``
    / ``events`` / ``history`` / ``detail``) until Priority 1's
    worker dispatch unification; coord omits ``delete`` /
    ``refresh_title`` / ``set_title`` / attachments.
    """

    # Listing
    list_workstreams: Handler | None = None  # GET  {prefix}
    list_saved: Handler | None = None  # GET  {prefix}/saved

    # Create
    create: Handler | None = None  # POST {prefix}/new

    # Per-``{ws_id}`` lifecycle
    detail: Handler | None = None  # GET  {prefix}/{ws_id}
    delete: Handler | None = None  # POST {prefix}/{ws_id}/delete
    open: Handler | None = None  # POST {prefix}/{ws_id}/open
    close: Handler | None = None  # POST {prefix}/{ws_id}/close
    refresh_title: Handler | None = None  # POST {prefix}/{ws_id}/refresh-title
    set_title: Handler | None = None  # POST {prefix}/{ws_id}/title

    # Legacy interactive close (``ws_id`` in body, not path) — the only
    # surviving body-keyed verb. Set non-``None`` to mount POST {prefix}/close.
    close_legacy: Handler | None = None

    # Per-``{ws_id}`` interaction (coord shape today; interactive
    # adopts these in Priority 1's worker dispatch unification)
    send: Handler | None = None  # POST {prefix}/{ws_id}/send
    dequeue: Handler | None = None  # DELETE {prefix}/{ws_id}/send
    approve: Handler | None = None  # POST {prefix}/{ws_id}/approve
    plan: Handler | None = None  # POST {prefix}/{ws_id}/plan
    cancel: Handler | None = None  # POST {prefix}/{ws_id}/cancel
    events: Handler | None = None  # GET  {prefix}/{ws_id}/events (SSE)
    history: Handler | None = None  # GET  {prefix}/{ws_id}/history

    # Attachments — the four handlers come together or not at all.
    attachments: AttachmentHandlers | None = None


@dataclass(frozen=True)
class CoordOnlyVerbHandlers:
    """Bundle of coord-only HTTP handler callables.

    These verbs read or mutate state that doesn't exist on interactive
    workstreams — children registry, parent quota, trust / restrict
    policy, cascade controls — so they live on a Protocol distinct
    from :class:`SharedSessionVerbHandlers`. Mounted at the same
    ``/api/workstreams/{ws_id}/`` prefix so the URL surface stays
    unified, but registered through a separate call so the kind
    separation is explicit at the wiring site.
    """

    children: Handler  # GET  {prefix}/{ws_id}/children
    tasks: Handler  # GET  {prefix}/{ws_id}/tasks
    metrics: Handler  # GET  {prefix}/{ws_id}/metrics
    trust: Handler  # POST {prefix}/{ws_id}/trust
    restrict: Handler  # POST {prefix}/{ws_id}/restrict
    stop_cascade: Handler  # POST {prefix}/{ws_id}/stop_cascade
    close_all_children: Handler  # POST {prefix}/{ws_id}/close_all_children


def register_session_routes(
    routes: list[BaseRoute],
    *,
    prefix: str,
    handlers: SharedSessionVerbHandlers,
) -> None:
    """Append the shared workstream HTTP route table to ``routes`` at ``prefix``.

    Mounts every verb whose handler is non-``None``. Routes register
    in an order that respects Starlette's first-match semantics:
    literal subpaths (``saved``, ``new``, ``close``) before the
    per-``{ws_id}`` patterns; per-``{ws_id}/{verb}`` patterns before
    the bare ``{ws_id}`` detail GET.

    ``prefix`` is the URL prefix relative to the mount, e.g.
    ``"/api/workstreams"``.
    """
    p = prefix.rstrip("/")

    # --- Listing endpoints ----------------------------------------------
    if handlers.list_workstreams is not None:
        routes.append(Route(p, handlers.list_workstreams))
    # Literal ``saved`` must register BEFORE the bare ``{ws_id}``
    # detail GET below so Starlette doesn't match "saved" as a ws_id.
    if handlers.list_saved is not None:
        routes.append(Route(f"{p}/saved", handlers.list_saved))

    # --- Lifecycle: create + legacy close (ws_id in body) ---------------
    if handlers.create is not None:
        routes.append(Route(f"{p}/new", handlers.create, methods=["POST"]))
    if handlers.close_legacy is not None:
        routes.append(Route(f"{p}/close", handlers.close_legacy, methods=["POST"]))

    # --- Per-``{ws_id}`` verbs (specific verbs first) -------------------
    if handlers.delete is not None:
        routes.append(Route(f"{p}/{{ws_id}}/delete", handlers.delete, methods=["POST"]))
    if handlers.open is not None:
        routes.append(Route(f"{p}/{{ws_id}}/open", handlers.open, methods=["POST"]))
    if handlers.close is not None:
        routes.append(Route(f"{p}/{{ws_id}}/close", handlers.close, methods=["POST"]))
    if handlers.refresh_title is not None:
        routes.append(
            Route(
                f"{p}/{{ws_id}}/refresh-title",
                handlers.refresh_title,
                methods=["POST"],
            )
        )
    if handlers.set_title is not None:
        routes.append(Route(f"{p}/{{ws_id}}/title", handlers.set_title, methods=["POST"]))
    if handlers.send is not None:
        routes.append(Route(f"{p}/{{ws_id}}/send", handlers.send, methods=["POST"]))
    if handlers.dequeue is not None:
        routes.append(Route(f"{p}/{{ws_id}}/send", handlers.dequeue, methods=["DELETE"]))
    if handlers.approve is not None:
        routes.append(Route(f"{p}/{{ws_id}}/approve", handlers.approve, methods=["POST"]))
    if handlers.plan is not None:
        routes.append(Route(f"{p}/{{ws_id}}/plan", handlers.plan, methods=["POST"]))
    if handlers.cancel is not None:
        routes.append(Route(f"{p}/{{ws_id}}/cancel", handlers.cancel, methods=["POST"]))
    if handlers.events is not None:
        routes.append(Route(f"{p}/{{ws_id}}/events", handlers.events, methods=["GET"]))
    if handlers.history is not None:
        routes.append(Route(f"{p}/{{ws_id}}/history", handlers.history, methods=["GET"]))

    # --- Attachments (the quartet comes together or not at all) ---------
    if handlers.attachments is not None:
        a = handlers.attachments
        routes.append(Route(f"{p}/{{ws_id}}/attachments", a.upload, methods=["POST"]))
        routes.append(Route(f"{p}/{{ws_id}}/attachments", a.list, methods=["GET"]))
        routes.append(
            Route(
                f"{p}/{{ws_id}}/attachments/{{attachment_id}}/content",
                a.get_content,
                methods=["GET"],
            )
        )
        routes.append(
            Route(
                f"{p}/{{ws_id}}/attachments/{{attachment_id}}",
                a.delete,
                methods=["DELETE"],
            )
        )

    # --- Bare ``{ws_id}`` detail (GET) registers LAST so the verb-
    #     suffixed patterns above win for ``{ws_id}/...`` paths.
    if handlers.detail is not None:
        routes.append(Route(f"{p}/{{ws_id}}", handlers.detail, methods=["GET"]))


def register_coord_verbs(
    routes: list[BaseRoute],
    *,
    prefix: str,
    handlers: CoordOnlyVerbHandlers,
) -> None:
    """Mount coord-only verbs at the unified ``{prefix}/{ws_id}/...`` shape.

    Call ordering vs :func:`register_session_routes` doesn't matter
    in practice — Starlette's default ``str`` path converter is
    single-segment, so ``{ws_id}/{verb}`` patterns can never collide
    with the bare ``{ws_id}`` detail GET registered by
    ``register_session_routes``.
    """
    p = prefix.rstrip("/")
    routes.append(Route(f"{p}/{{ws_id}}/children", handlers.children, methods=["GET"]))
    routes.append(Route(f"{p}/{{ws_id}}/tasks", handlers.tasks, methods=["GET"]))
    routes.append(Route(f"{p}/{{ws_id}}/metrics", handlers.metrics, methods=["GET"]))
    routes.append(Route(f"{p}/{{ws_id}}/trust", handlers.trust, methods=["POST"]))
    routes.append(Route(f"{p}/{{ws_id}}/restrict", handlers.restrict, methods=["POST"]))
    routes.append(Route(f"{p}/{{ws_id}}/stop_cascade", handlers.stop_cascade, methods=["POST"]))
    routes.append(
        Route(
            f"{p}/{{ws_id}}/close_all_children",
            handlers.close_all_children,
            methods=["POST"],
        )
    )


# ---------------------------------------------------------------------------
# Lifted handler bodies — Stage 2 Priority 0 body-convergence
#
# Each verb here was previously implemented twice (once in
# ``turnstone/server.py`` for interactive, once in
# ``turnstone/console/server.py`` for coord). The lifted body
# branches on the kind-specific :class:`SessionEndpointConfig` the
# factory captured at app-construction time.
#
# Verbs not lifted yet (intentional — bodies have substantive
# behavior divergence that needs SessionManager-side refactoring,
# not just kind branching): send (worker dispatch — Priority 1
# territory), cancel (interactive does inline forensics + force-
# cancel ws._lock manipulation), open (interactive resume vs coord
# rehydrate), events (different SSE replay shapes), create
# (interactive attachments vs coord initial_message), list / saved
# (different response keys: ``workstreams`` vs ``coordinators``).
# ---------------------------------------------------------------------------


def make_approve_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/approve``.

    Resolves a pending tool approval on the workstream's UI. Both
    kinds expose the same approve / feedback / always body shape and
    the same ``ui.resolve_approval(approved, feedback)`` mechanic;
    differences are auth scope, manager lookup, and the
    ``__budget_override__`` filter (interactive-only — coord workstreams
    don't have the budget-override pseudo-tool).
    """
    from turnstone.core.web_helpers import read_json_or_400

    async def approve(request: Request) -> Response:
        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        # ``manager_lookup`` returns ``(None, JSONResponse)`` when the
        # subsystem is unavailable (returned above) or
        # ``(SessionManager, None)`` otherwise; ``cast`` makes the
        # type-checker-only narrowing explicit and survives ``python -O``.
        mgr = cast("SessionManager", mgr_opt)
        body = await read_json_or_400(request)
        if isinstance(body, JSONResponse):
            return body
        ws_id = request.path_params.get("ws_id", "")
        approved = bool(body.get("approved", False))
        feedback = body.get("feedback")
        always = bool(body.get("always", False))
        if cfg.tenant_check is not None:
            err_tenant = cfg.tenant_check(request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant
        ws = mgr.get(ws_id)
        if ws is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        ui = ws.ui
        if ui is None or not hasattr(ui, "resolve_approval"):
            return JSONResponse(
                {"error": "session UI does not support approval"},
                status_code=409,
            )
        # ``_pending_approval`` and ``auto_approve_tools`` aren't on the
        # ``SessionUI`` Protocol — both interactive ``WebUI`` and
        # ``ConsoleCoordinatorUI`` add them, but a kind-agnostic body
        # has to look them up dynamically. The CLI ``CliUI`` wouldn't
        # have either, so accessing through ``getattr`` is also safer.
        pending = getattr(ui, "_pending_approval", None)
        auto_approve_tools = getattr(ui, "auto_approve_tools", None)
        if always and approved and pending and auto_approve_tools is not None:
            tool_names: set[str] = {
                it.get("approval_label", "") or it.get("func_name", "")
                for it in pending.get("items", [])
                if it.get("needs_approval") and it.get("func_name") and not it.get("error")
            }
            tool_names.discard("")
            # Budget-override is an interactive-only pseudo-tool that
            # must never be added to the auto-approve set — discarding
            # unconditionally is safe (no-op for coord).
            tool_names.discard("__budget_override__")
            if tool_names:
                auto_approve_tools.update(tool_names)
        ui.resolve_approval(approved, feedback)
        return JSONResponse({"status": "ok"})

    return approve


def make_legacy_body_keyed_adapter(handler: Handler) -> Handler:
    """Wrap a path-keyed handler so it can be mounted at a body-keyed URL.

    Pre-1.5 interactive handlers (``/api/approve``, ``/api/cancel``,
    ``/api/plan``, etc.) take ``ws_id`` from the JSON body. The
    lifted bodies in this module read ``ws_id`` from the path. This
    adapter peeks the body for ``ws_id``, copies it into
    ``request.path_params``, and forwards. Starlette caches the
    request body so the lifted handler's own body read is a hash-map
    lookup, not a second network read.
    """

    async def adapter(request: Request) -> Response:
        from turnstone.core.web_helpers import read_json_or_400

        body = await read_json_or_400(request)
        if isinstance(body, JSONResponse):
            return body
        ws_id = str(body.get("ws_id") or "")
        # ``request.path_params`` is normally populated by Starlette
        # at Route match time; since this adapter is mounted on a
        # body-keyed URL with no ``{ws_id}`` slot, we splice it into
        # the scope so the lifted handler's ``request.path_params.get(...)``
        # finds it.
        path_params: dict[str, Any] = dict(request.path_params)
        path_params["ws_id"] = ws_id
        request.scope["path_params"] = path_params
        return await handler(request)

    return adapter


def make_legacy_query_keyed_adapter(handler: Handler) -> Handler:
    """Wrap a path-keyed handler so it can be mounted at a query-keyed URL.

    Pre-1.5 interactive ``GET /api/events?ws_id=...`` takes ``ws_id``
    from the query string. The lifted ``events`` body reads it from
    the path. This adapter mirrors :func:`make_legacy_body_keyed_adapter`
    for the query-param case: read ``ws_id`` from the query, splice
    into path_params, forward.

    SSE-friendly: no body read involved (events is GET); the handler
    stays streaming.
    """

    async def adapter(request: Request) -> Response:
        ws_id = request.query_params.get("ws_id", "")
        path_params: dict[str, Any] = dict(request.path_params)
        path_params["ws_id"] = ws_id
        request.scope["path_params"] = path_params
        return await handler(request)

    return adapter


CloseAuditEmitter = Callable[
    ["Request", str, "Workstream", str],
    None,
]


def make_close_handler(
    cfg: SessionEndpointConfig,
    *,
    audit_emit: CloseAuditEmitter | None = None,
    supports_close_reason: bool = False,
) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/close``.

    Closes the workstream's session (unloads from memory; storage row
    survives so the session can be re-opened later). Both kinds share
    the same auth → mgr → ws-lookup → ``mgr.close()`` → audit
    sequence; per-kind divergence is in the audit detail shape and
    whether a request body ``reason`` is read / capped / persisted on
    the workstream's config row.

    Args:
        cfg: per-kind policy bundle (auth, manager lookup, tenant
            check, error labels). Captured by closure so the request-
            time handler doesn't reach into ``app.state``.
        audit_emit: kind's audit emitter for the close event.
            Receives ``(request, ws_id, ws_before, reason)``; ``reason``
            is the empty string when ``supports_close_reason`` is
            ``False`` or no reason was provided. ``None`` skips the
            audit entirely (only valid when neither kind cares).
        supports_close_reason: when ``True``, the handler reads a
            ``reason`` field from the JSON body, caps it at 512 UTF-8
            bytes, redacts credentials, persists it via
            ``storage.save_workstream_config(ws_id, {"close_reason": ...})``,
            and threads it through to ``audit_emit``. The cap protects
            ``workstream_config`` from unbounded growth on a model-
            generated dump; the redact protects audit logs from
            captured-secret leakage under prompt injection.

    Behavior change vs the pre-lift handlers:

    - The interactive handler previously let ``record_audit`` failures
      surface as HTTP 500 (no try/except). The lifted body wraps
      ``audit_emit`` in try/except and demotes failures to a
      ``warning`` log, returning 200 to the caller. Coord previously
      already swallowed; convergence is intentional — operators
      monitor the audit-fail log line in both kinds the same way.
    - The coord ``mgr.close()`` race-loss returned 500; standardized
      to 404 ("popped between ``.get()`` and ``.close()``" is a
      not-found semantic, not a server error).
    """

    async def close(request: Request) -> Response:
        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        # See ``make_approve_handler`` for the cast rationale.
        mgr = cast("SessionManager", mgr_opt)
        ws_id = request.path_params.get("ws_id", "")

        reason = ""
        if supports_close_reason:
            from turnstone.core.output_guard import redact_credentials
            from turnstone.core.web_helpers import read_json_or_400

            body = await read_json_or_400(request)
            if isinstance(body, JSONResponse):
                return body
            raw_reason = body.get("reason", "")
            if isinstance(raw_reason, str):
                # Cap on UTF-8 bytes (not code points) so a CJK / emoji
                # payload can't sneak past at 3-4x the documented budget.
                # ``errors="ignore"`` drops any partial code point left
                # at the truncation boundary.
                capped = raw_reason.strip().encode("utf-8")[:512].decode("utf-8", errors="ignore")
                reason = redact_credentials(capped)

        if cfg.tenant_check is not None:
            err_tenant = cfg.tenant_check(request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws_before = mgr.get(ws_id)
        if ws_before is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        if not mgr.close(ws_id):
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)

        storage = getattr(request.app.state, "auth_storage", None)
        if supports_close_reason and reason and storage is not None:
            try:
                storage.save_workstream_config(ws_id, {"close_reason": reason})
            except Exception:
                log.warning(
                    "ws.close.reason_persist_failed ws=%s",
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )

        if audit_emit is not None and storage is not None:
            try:
                audit_emit(request, ws_id, ws_before, reason)
            except Exception:
                # Audit-write failure is a compliance signal —
                # ``warning`` so it surfaces in ops logs. Behavior change
                # vs the original interactive handler (which would have
                # 500'd here); see the function docstring.
                log.warning(
                    "ws.close.audit_failed ws=%s",
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )

        return JSONResponse({"status": "ok"})

    return close


CancelAuditEmitter = Callable[
    ["Request", str, "Workstream", bool],
    None,
]


def make_cancel_handler(
    cfg: SessionEndpointConfig,
    *,
    audit_emit: CancelAuditEmitter | None = None,
) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/cancel``.

    Cancels in-flight generation on a workstream. Sets the cooperative
    cancel flag on the session, unblocks any pending approval / plan
    waits, and (when the request body asks for it) force-abandons a
    stuck worker thread so the UI recovers immediately.

    Both kinds share the cancel sequence (``session.cancel`` →
    ``ui.resolve_approval(False)`` → ``ui.resolve_plan("reject")``).
    Per-kind divergence captured via the cfg + ``audit_emit``:

    - ``cancel_forensics`` (cfg) — when set, the lifted body calls
      it with ``(session, ui, was_running=...)`` and surfaces the
      result as the response's ``dropped`` key. Interactive wires
      ``_capture_cancel_forensics`` so the model-invoked
      ``cancel_workstream`` tool can tell operators what got killed
      (pending-approval tool names, queued-message preview); coord
      wires ``None`` and the response's ``dropped`` is ``{}``.
    - ``audit_emit`` — receives ``(request, ws_id, ws, force)``.
      Coord wires its ``coordinator.cancel`` audit hook; interactive
      wires ``None`` (cancel isn't audited on interactive today —
      preserved for behavioural parity with the pre-lift handler).

    Args:
        cfg: per-kind policy bundle (auth, manager lookup, tenant
            check, error labels, ``cancel_forensics``).
        audit_emit: kind's audit emitter for the cancel event.
            ``None`` skips the audit entirely.

    Behavior changes vs the pre-lift handlers:

    - **Coord gains the ``force`` flag.** Pre-lift coord ignored
      ``force``; the lifted body honours it on both kinds (parity
      gain — coord workers can hang the same way interactive's can,
      and operators benefit from the same recovery path).
    - **Coord response shape now includes ``dropped: {}``.**
      Pre-lift coord returned bare ``{"status": "ok"}``; the unified
      shape always carries ``dropped`` so SDK consumers don't have
      to branch on kind. Coord's ``dropped`` is ``{}`` until coord
      grows its own forensic capture.
    - **Coord cancel returns 400 when ``ws.session is None``** (the
      placeholder/build-failed path). Pre-lift coord called
      ``coord_mgr.cancel`` which silently no-op'd on a placeholder;
      the lifted body 400s for parity with interactive's existing
      "No session" branch.
    - **Interactive ``resolve_plan`` now runs on every cancel** (was
      gated on ``was_running``). Lifts coord's always-resolve
      behaviour onto interactive — a stuck plan-pending state from
      a crashed worker thread can now be cleared via ``cancel``,
      matching coord's pre-lift recovery path. ``resolve_plan`` has
      its own internal ``_pending_plan_review is None`` guard, so
      the call is genuinely no-op when nothing is blocked.
      ``resolve_approval`` is **gated on ``ui._pending_approval is not None``**
      because :meth:`SessionUIBase.resolve_approval` is *not*
      idempotent — it always broadcasts ``approval_resolved`` and
      overwrites ``_approval_result``. Without the gate, every idle
      cancel would leak a stale resolution event to SSE listeners.
      The gate preserves the recovery semantics for the genuine
      stuck case while skipping the broadcast on idle cancels.
    """

    async def cancel(request: Request) -> Response:
        from turnstone.core.web_helpers import read_json_or_400

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        # See ``make_approve_handler`` for the cast rationale.
        mgr = cast("SessionManager", mgr_opt)
        ws_id = request.path_params.get("ws_id", "")

        # Body is optional — only ``force`` is read. An empty body is
        # a valid cancel request (the original coord URL took no body
        # at all; preserve that ergonomic). Malformed JSON is treated
        # as no body rather than 400'd: cancel is a recovery verb and
        # should work even when the caller's JSON is junk.
        force = False
        try:
            body = await read_json_or_400(request)
        except Exception:
            body = None
        if isinstance(body, dict):
            force = body.get("force", False) is True

        if cfg.tenant_check is not None:
            err_tenant = cfg.tenant_check(request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws = mgr.get(ws_id)
        if ws is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        session = ws.session
        ui = ws.ui
        if session is None or ui is None:
            return JSONResponse({"error": "No session"}, status_code=400)

        was_running = bool(getattr(ws, "_worker_running", False))
        dropped: dict[str, Any] = {}
        if cfg.cancel_forensics is not None:
            try:
                dropped = cfg.cancel_forensics(session, ui, was_running=was_running)
            except Exception:
                # Forensics is observational — never let a snapshot
                # bug block the actual cancel. Log and proceed with
                # an empty dropped dict.
                log.debug("ws.cancel.forensics_failed ws=%s", ws_id[:8], exc_info=True)
                dropped = {}

        # Always set the cooperative cancel flag — cheap, no harm if
        # nothing's running. resolve_approval / resolve_plan are
        # gated by their respective ``_pending_*`` slots: pre-lift
        # coord called them unconditionally via ``mgr.cancel`` (which
        # is recovery-friendly: a stuck approval-pending state from a
        # crashed worker can still be cleared), but ``resolve_approval``
        # is NOT idempotent — calling it with no pending approval
        # broadcasts a stale ``approval_resolved`` SSE event and
        # overwrites ``_approval_result``. Gating on the pending slot
        # preserves the recovery semantics for the actual stuck case
        # while skipping the broadcast on idle cancels. ``resolve_plan``
        # has its own internal no-pending guard, so the call is
        # already safe to make unconditionally.
        try:
            session.cancel()
        except Exception:
            log.debug("ws.cancel.session_failed ws=%s", ws_id[:8], exc_info=True)
        if hasattr(ui, "resolve_approval") and getattr(ui, "_pending_approval", None) is not None:
            try:
                ui.resolve_approval(False, "Cancelled by user")
            except Exception:
                log.debug(
                    "ws.cancel.resolve_approval_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )
        if hasattr(ui, "resolve_plan"):
            try:
                ui.resolve_plan("reject")
            except Exception:
                log.debug(
                    "ws.cancel.resolve_plan_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )

        # The remaining steps only matter when a worker is actually
        # running: force-recovery has nothing to recover otherwise,
        # and the SSE ``cancelled`` event would mislead consumers that
        # have no in-flight generation to cancel.
        if was_running:
            if force:
                # Force cancel: abandon the stuck worker thread (daemon,
                # will die on process exit or stream timeout) and emit
                # stream_end so the UI and session recover immediately.
                # The per-generation cancel flag stays set so the
                # abandoned thread still kills subprocesses at its next
                # checkpoint. Clear ``_worker_running`` alongside
                # ``worker_thread`` so a follow-up send doesn't see the
                # ``(_worker_running=True, worker_thread=None)``
                # half-state and route through ``enqueue()`` to the
                # abandoned worker's queue (which won't drain — the
                # cancel flag short-circuits the abandoned thread
                # before it reaches the queue-drain seam, leaving the
                # queued message orphaned until the next spawn).
                # ``session_worker.send`` documents this invariant:
                # "readers gating on either flag see a coherent
                # (worker_thread, _worker_running) pair."
                with ws._lock:
                    ws.worker_thread = None
                    ws._worker_running = False
                if hasattr(ui, "_enqueue"):
                    try:
                        ui._enqueue({"type": "stream_end"})
                    except Exception:
                        log.debug(
                            "ws.cancel.stream_end_failed ws=%s",
                            ws_id[:8],
                            exc_info=True,
                        )
                if hasattr(ui, "on_state_change"):
                    try:
                        ui.on_state_change("idle")
                    except Exception:
                        log.debug(
                            "ws.cancel.idle_state_failed ws=%s",
                            ws_id[:8],
                            exc_info=True,
                        )
            elif hasattr(ui, "_enqueue"):
                try:
                    ui._enqueue({"type": "cancelled"})
                except Exception:
                    log.debug(
                        "ws.cancel.cancelled_event_failed ws=%s",
                        ws_id[:8],
                        exc_info=True,
                    )

        if audit_emit is not None:
            try:
                audit_emit(request, ws_id, ws, force)
            except Exception:
                # Mirrors make_close_handler — audit-write failures
                # shouldn't surface as HTTP 500. Log + continue.
                log.warning(
                    "ws.cancel.audit_failed ws=%s",
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )

        return JSONResponse({"status": "ok", "dropped": dropped})

    return cancel


def make_open_handler(
    cfg: SessionEndpointConfig,
    *,
    audit_emit: OpenAuditEmitter | None = None,
) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/open``.

    Loads a persisted workstream into memory under its original
    ws_id (vs ``resume`` which forks into a fresh ws_id). Both kinds
    share the auth → mgr → already-loaded shortcut → ``mgr.open()``
    → 404-on-miss sequence; per-kind divergence captured by the
    cfg + ``audit_emit``:

    - ``cfg.open_resolve_alias`` — interactive wires
      :func:`turnstone.core.memory.resolve_workstream` so callers
      can pass user-friendly aliases ("my-debug-ws") in the path
      param. Coord wires ``None`` (hex ids only).
    - ``cfg.open_post_load`` — interactive uses it for UI-replay
      events (``clear_ui`` + history) plus a handler-side
      ``ws_created`` enqueue onto the global SSE queue. Coord wires
      ``None`` and relies on the cluster collector fan-out triggered
      by ``CoordinatorAdapter.emit_rehydrated``.
    - ``audit_emit`` — kind's audit hook for the ``open`` event.
      Interactive wires ``workstream.opened``; coord wires ``None``
      (coord doesn't audit open today).

    Pre-lift behaviour preserved on both kinds with one important
    fix: **interactive previously called ``mgr.create(ws_id=...)``
    + ``ws.session.resume(...)`` to rehydrate, bypassing
    ``mgr.open()`` entirely**. After this lift both kinds route
    through ``mgr.open()`` — which makes ``emit_rehydrated``
    reachable on interactive (it had been dead-by-routing pre-lift)
    and gives the manager a single rehydrate code path to maintain.
    See § Post-P3 reckoning item #3 in
    ``1.5.0-session-manager-stage-2.md`` for the design history.

    Args:
        cfg: per-kind policy bundle.
        audit_emit: kind's audit hook. ``None`` skips the audit.
    """

    async def open_ws(request: Request) -> Response:
        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        # See ``make_approve_handler`` for the cast rationale.
        mgr = cast("SessionManager", mgr_opt)

        ws_id = request.path_params.get("ws_id", "")
        if not ws_id:
            return JSONResponse({"error": "ws_id is required"}, status_code=400)

        # Optional alias resolution. Interactive lets callers pass
        # user-friendly aliases; coord skips this entirely.
        if cfg.open_resolve_alias is not None:
            resolved = cfg.open_resolve_alias(ws_id)
            if not resolved:
                return JSONResponse({"error": cfg.not_found_label}, status_code=404)
            ws_id = resolved

        # Already-loaded shortcut — both kinds return the same
        # ``{ws_id, name, already_loaded: true}`` shape.
        existing = mgr.get(ws_id)
        if existing is not None:
            return JSONResponse(
                {
                    "ws_id": existing.id,
                    "name": existing.name,
                    "already_loaded": True,
                }
            )

        try:
            ws = mgr.open(ws_id)
        except ValueError as exc:
            # Session factory misconfig (e.g., a model alias that
            # no longer exists). Surface the factory's remediation
            # text as a 503 so the operator can fix it without
            # digging through stack traces. Same shape coord used
            # pre-lift; standardised across both kinds here.
            return JSONResponse({"error": str(exc)}, status_code=503)
        except Exception:
            # Bare ``Exception`` is intentional: ``mgr.open`` can
            # raise from ``adapter.build_session`` (no documented
            # exception spec — depends on the kind's session factory)
            # or from ``ChatSession.resume`` propagating a partial-
            # restore failure (corrupted workstream_config row,
            # model-registry mismatch on saved alias, etc.). Either
            # way the workstream isn't loadable; the operator needs
            # the correlation-id'd log entry to diagnose.
            #
            # Don't echo the exception text — it can leak internal
            # paths / frame names. Log with a correlation id and
            # return that to the client so support can match a
            # report to the log line. Mirrors coord's pre-lift
            # ``coordinator_open`` 500 path.
            import secrets

            correlation_id = secrets.token_hex(4)
            log.warning(
                "ws.open.rehydrate_failed correlation_id=%s ws_id=%s",
                correlation_id,
                ws_id[:8] if ws_id else "",
                exc_info=True,
            )
            # Per-kind noun in the user-facing error so coord callers
            # see "failed to open coordinator" and interactive callers
            # see "failed to open workstream" (matching the pre-lift
            # ``coordinator_open`` / ``open_workstream`` wording on
            # both sides). ``audit_action_prefix`` is the existing
            # per-kind label both lifespans already construct
            # ("workstream" / "coordinator"); reusing it here gives
            # the cfg field its first runtime reader.
            kind_noun = cfg.audit_action_prefix or "workstream"
            return JSONResponse(
                {
                    "error": (
                        f"failed to open {kind_noun} (internal error). "
                        f"correlation_id={correlation_id}"
                    )
                },
                status_code=500,
            )

        # Both except branches above ``return``; ``ws`` is bound here.
        if ws is None:
            # ``mgr.open`` returns None for missing rows, kind
            # mismatch, and tombstoned rows — all surface as 404
            # for the caller (the kind-specific failure mode is
            # internal detail).
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)

        # Kind-specific post-load action (interactive: UI replay +
        # handler-side ws_created enqueue; coord: None and the
        # cluster collector handles the fan-out via the adapter's
        # emit_rehydrated path).
        if cfg.open_post_load is not None:
            try:
                cfg.open_post_load(request, ws)
            except Exception:
                # Post-load is observational — never let a hook bug
                # block the open. Log + continue.
                log.debug(
                    "ws.open.post_load_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )

        if audit_emit is not None:
            try:
                audit_emit(request, ws)
            except Exception:
                # Mirrors make_close_handler / make_cancel_handler
                # — audit-write failures shouldn't surface as HTTP
                # 500. Log + continue.
                log.warning(
                    "ws.open.audit_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )

        return JSONResponse({"ws_id": ws.id, "name": ws.name})

    return open_ws


def make_events_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``GET {prefix}/{ws_id}/events`` — per-workstream SSE.

    Both kinds share the SSE plumbing: register the per-UI listener
    queue, run the kind-specific initial replay (``cfg.events_replay``,
    typically ``connected`` + ``status`` + ``history`` + pending
    approval / plan on interactive; just pending approval / plan on
    coord), then drain the queue forever until either the workstream
    closes (``ws_closed`` event) or the client disconnects.

    The kind-specific divergence is captured entirely by
    ``cfg.events_replay``. The live-loop body, the listener
    registration, the ``ws_closed`` exit, the disconnect detection,
    and the SSE-connect/disconnect metric recording are uniform.

    Pre-lift behaviour preserved on both kinds with two small
    convergence wins:

    - **Coord gains SSE connect/disconnect metrics.** Pre-lift coord
      did no metric recording on its events stream; the lifted body
      always calls ``metrics.record_sse_connect()`` / ``...disconnect()``,
      which gives the cluster dashboard the same per-stream
      observability interactive's had since 1.0.
    - **Both kinds now check ``request.is_disconnected()`` between
      polls AND the ``ws_closed`` event.** Pre-lift interactive
      relied solely on ``ws_closed`` to terminate (which never fires
      if the client just goes away without a proper close); pre-lift
      coord relied solely on ``is_disconnected``. The lifted body
      uses both — whichever fires first wins.

    Args:
        cfg: per-kind policy bundle. ``events_replay`` is the only
             field the events body reads beyond the standard
             permission_gate / manager_lookup / tenant_check prelude.
    """
    # Lazy-imported at factory call time so the metrics module isn't
    # dragged into ``session_routes.py``'s top-level import graph
    # (which is consumed by the ``client_type="chat"`` channel
    # gateway, where the metrics collector is irrelevant).
    from turnstone.core.metrics import metrics as _metrics

    async def events(request: Request) -> Response:
        import asyncio
        import json
        import queue

        from sse_starlette import EventSourceResponse

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        # See ``make_approve_handler`` for the cast rationale.
        mgr = cast("SessionManager", mgr_opt)

        ws_id = request.path_params.get("ws_id", "")
        if not ws_id:
            return JSONResponse({"error": "ws_id is required"}, status_code=400)

        if cfg.tenant_check is not None:
            err_tenant = cfg.tenant_check(request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws = mgr.get(ws_id)
        if ws is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        ui = ws.ui
        # The listener-queue methods aren't on the ``SessionUI``
        # Protocol surface (they live on ``SessionUIBase``), so
        # extract via ``getattr`` after presence checks. Both kinds'
        # production UIs subclass ``SessionUIBase``; the placeholder /
        # build-failed UI path may have neither.
        register = getattr(ui, "_register_listener", None) if ui is not None else None
        unregister = getattr(ui, "_unregister_listener", None) if ui is not None else None
        if ui is None or register is None or unregister is None:
            # Placeholder / build-failed UI — there's no listener
            # queue to attach to. 409 (not 404) because the
            # workstream EXISTS in the manager but its UI is half-built.
            # Pre-lift coord returned 409 for this case; pre-lift
            # interactive 404'd. Lifted converges on 409 across
            # kinds — more accurate for the workstream-exists-but-
            # half-built shape.
            return JSONResponse({"error": "session has no UI"}, status_code=409)

        client_queue = register()

        # Per-kind executor for the blocking ``client_queue.get``
        # wait. Interactive returns its dedicated 200-thread
        # ``sse_executor`` so SSE polling stays isolated from every
        # other ``asyncio.to_thread`` caller in the process; coord
        # returns ``None`` and the lifted body falls back to the
        # default executor (capped at ``min(32, cpu+4)``). Pre-lift
        # interactive used the dedicated pool too — the lookup
        # restores that isolation under the lifted contract.
        live_executor = (
            cfg.sse_executor_lookup(request) if cfg.sse_executor_lookup is not None else None
        )
        # Capture the replay callback in a local so the inner
        # generator's closure doesn't have to re-read the cfg field.
        replay_cb = cfg.events_replay

        async def event_generator() -> Any:
            import functools

            _metrics.record_sse_connect()
            loop = asyncio.get_running_loop()
            try:
                # Replay phase — stream the kind-specific initial
                # payload one event at a time so the client sees the
                # first byte immediately (interactive's ``connected``
                # event is the very first yield, before the heavier
                # ``status`` / ``history`` work runs). Pre-building
                # the replay into a list would block time-to-first-
                # byte until the entire replay materialized AND let
                # the listener queue accumulate (potentially over its
                # 500-slot cap on a chatty mid-generation workstream)
                # while replay was being built.
                if replay_cb is not None:
                    try:
                        for ev in replay_cb(ws, ui, request):
                            yield {"data": json.dumps(ev)}
                    except Exception:
                        # Replay is observational — never let a
                        # snapshot bug block the live stream. Log
                        # and continue with whatever partial replay
                        # was already yielded.
                        log.debug(
                            "ws.events.replay_failed ws=%s",
                            ws_id[:8],
                            exc_info=True,
                        )
                # Live phase — drain the per-UI listener queue
                # until either the workstream closes or the client
                # disconnects. 5s poll matches pre-lift interactive
                # (the ``is_disconnected`` probe between polls covers
                # cancel-detection latency the timeout would otherwise
                # gate; shortening to 1s 5x'd the wakeup rate without
                # any client-observable benefit).
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await loop.run_in_executor(
                            live_executor,
                            functools.partial(client_queue.get, timeout=5),
                        )
                    except queue.Empty:
                        continue  # ping keeps the connection alive
                    if event.get("type") == "ws_closed":
                        return
                    yield {"data": json.dumps(event)}
            finally:
                _metrics.record_sse_disconnect()
                unregister(client_queue)

        return EventSourceResponse(event_generator(), ping=5)

    return events


def make_create_handler(
    cfg: SessionEndpointConfig,
    *,
    audit_emit: CreateAuditEmitter | None = None,
) -> Handler:
    """Lifted body for ``POST {prefix}/new`` — workstream creation.

    Both kinds share the create sequence (parse body → resolve uid
    → kind-specific validate → resolve skill → ``mgr.create`` (with
    ``defer_emit_created=True``) → save attachments → ``mgr.discard``
    on validation failure / ``mgr.commit_create`` on success → audit
    → kind-specific post-install → respond). Per-kind divergence
    captured by the cfg + ``audit_emit``:

    - ``cfg.create_supports_attachments`` — when ``True``, the body
      may arrive as ``multipart/form-data`` with a ``meta`` JSON
      field + ``file`` parts; uploads are validated post-create and
      the workstream is rolled back if any file fails (interactive's
      pre-lift pattern, lifted to coord here for parity).
    - ``cfg.create_supports_user_id_override`` — when ``True``, a
      ``user_id`` body field overrides the auth-derived uid if the
      auth token is from a trusted service. Interactive ``True`` so
      console-proxied creates carry the real end-user identity;
      coord ``False``.
    - ``cfg.create_validate_request`` — kind-specific pre-create
      gates (interactive: ws_id format, kind, parent_ws_id ownership,
      attachments+resume_ws combo; coord: 401-on-empty-uid).
    - ``cfg.create_build_kwargs`` — kind-specific kwargs for
      ``mgr.create``. Required when the kind mounts a create handler.
    - ``cfg.create_post_install`` — kind-specific tail end (e.g.
      interactive's resume + skill_config + initial-message worker
      thread; coord's initial_message via coord_adapter.send).
    - ``audit_emit`` — ``workstream.created`` on interactive,
      ``coordinator.create`` on coord.

    Ordering invariants (load-bearing — easy to break in a refactor):

    1. ``mgr.create(defer_emit_created=True)`` runs FIRST so the
       slot + storage row + session exist before any post-create
       work touches them.
    2. Attachment validation runs BEFORE ``commit_create`` so a
       rejected upload produces zero lifecycle events. Failure path
       is ``mgr.discard`` + ``delete_workstream``; success path
       falls through.
    3. ``mgr.commit_create(ws)`` runs BEFORE ``audit_emit`` and
       ``post_install`` so any state-change events ``post_install``
       triggers (e.g. a worker dispatched on ``initial_message``)
       reach the cluster collector for an already-known ws_id.
       Reordering this commit after the worker dispatch puts
       ``emit_state`` on the wire ahead of ``emit_created``.

    Behavior changes vs the pre-lift handlers (documented in
    CHANGELOG, mostly coord-up-to-interactive parity gains):

    - **Coord gains create-time attachments.** Pre-lift
      ``coordinator_create`` accepted JSON only and ignored uploads;
      the lifted body parses multipart bodies on coord and saves
      attachments through the kind-agnostic storage layer (§ Post-P3
      reckoning item #1). When the same request supplies an
      ``initial_message``, the uploads are reserved onto the
      dispatched first turn via ``CoordinatorAdapter.send`` (which
      gained ``attachments`` + ``send_id`` kwargs in the same
      release).
    - **No phantom create→close pair on coord rollback.** The lifted
      body now passes ``defer_emit_created=True`` to ``mgr.create``
      and explicitly fires ``mgr.commit_create(ws)`` only after
      attachment validation passes. On failure ``mgr.discard(ws.id)``
      releases the slot WITHOUT firing ``emit_closed`` (because the
      create was never advertised). Pre-fix, coord's ``mgr.create``
      fired ``emit_created`` synchronously and a rollback then
      called ``mgr.close``, surfacing a quick create→close pair on
      the cluster events stream that consumers had to reconcile via
      the collector's diff path. Post-fix, a rejected upload
      produces zero events. Interactive's ``emit_created`` is a
      documented no-op stub so the deferral is observably a no-op
      there; the ``ws_created`` broadcast on the global SSE queue
      continues to fire from the kind's post_install callback.
    - **Coord gains the disabled-skill rejection.** Pre-lift
      ``coordinator_create`` silently allowed disabled skills to
      flow through to ``mgr.create``; the lifted body returns 400
      ("Skill not found or disabled") matching interactive's
      behaviour. Disabled skills are inert by definition; the gate
      makes that explicit.
    - **Both kinds converge on 200 OK.** Pre-lift interactive
      returned 200 (default); coord returned 201. SDK consumers
      that were branching on ``response.status == 201`` on coord
      should switch to ``response.ok``. 200 was picked over 201 for
      response-shape parity with the rest of the v1 surface (every
      other shared verb returns 200), at the cost of leaving REST-
      strictly-correct semantics on the table — a one-time release
      note rather than ongoing client churn.
    - **Both kinds converge on the manager-at-capacity 429
      semantic.** Pre-lift interactive translated mgr.create's
      ``RuntimeError`` to 400 ("invalid create request"); coord
      already translated to 429. RuntimeError on ``SessionManager.create``
      is documented as "manager at capacity" — 429 (rate-limit /
      try-later) is the correct shape for both.
    - **Both kinds converge on the factory-misconfig 503
      semantic.** Pre-lift interactive let ``ValueError`` (raised by
      the session factory on a misconfigured model alias) propagate
      as 500; coord already translated to 503. The lifted body uses
      503 with the factory's remediation text on both kinds —
      operators get the actionable message instead of a generic
      stack-traced 500.
    - **Both kinds get a correlation_id'd 500 on unexpected
      ``mgr.create`` failure.** Pre-lift interactive let unexpected
      exceptions propagate as 500 with a stack-traced response
      (potential information leak); coord already returned a
      correlation_id'd 500 with the message redacted. The lifted
      body adopts coord's safer pattern on both kinds.
    - **Audit-emit failures no longer 500.** Pre-lift interactive
      audit failures surfaced as HTTP 500 (no try/except); coord
      swallowed via try/except + log.debug. The lifted body wraps
      ``audit_emit`` in try/except + ``warning`` log, returning the
      successful 200 to the caller. Mirrors the close / cancel /
      open lift contracts.
    - **Always-include response shape.** The lifted body always
      returns ``{ws_id, name, resumed, message_count, attachment_ids}``,
      with the parity fields defaulting to ``False`` / ``0`` / ``[]``
      on kinds whose post-install doesn't populate them. SDK
      consumers don't branch on kind.

    Args:
        cfg: per-kind policy bundle.
        audit_emit: kind's audit emitter for the create event.
            ``None`` skips the audit entirely.
    """
    # Lazy-imported at factory call time (mirrors the events lift) so
    # ``session_routes.py``'s top-level import graph stays tight.

    async def create(request: Request) -> Response:
        import asyncio
        import contextlib
        import secrets

        from turnstone.core.attachments import (
            IMAGE_SIZE_CAP,
            validate_and_save_uploaded_files,
        )
        from turnstone.core.web_helpers import (
            read_json_or_400,
            read_multipart_create_or_400,
        )

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        # See ``make_approve_handler`` for the cast rationale.
        mgr = cast("SessionManager", mgr_opt)

        # --- Body parsing -------------------------------------------------
        # Multipart only when the cfg lights up attachments AND the
        # caller actually sent a multipart body. Plain JSON stays the
        # default content type for both kinds.
        content_type = (request.headers.get("content-type") or "").lower()
        uploaded_files: list[tuple[str, str, bytes]] = []
        body: dict[str, Any]
        if cfg.create_supports_attachments and content_type.startswith("multipart/form-data"):
            # Multipart cap: up to MAX_PENDING × image cap, plus slack
            # for JSON meta + multipart framing. Per-file size is
            # enforced inside :func:`validate_and_save_uploaded_files`
            # against the kind-specific cap.
            parsed = await read_multipart_create_or_400(
                request,
                max_files=10,
                max_per_file_bytes=IMAGE_SIZE_CAP,
                max_total_bytes=10 * IMAGE_SIZE_CAP,
            )
            if isinstance(parsed, JSONResponse):
                return parsed
            body, uploaded_files = parsed
        else:
            json_body = await read_json_or_400(request)
            if isinstance(json_body, JSONResponse):
                return json_body
            body = json_body

        # --- User id resolution ------------------------------------------
        # Auth middleware populates ``request.state.auth_result`` for
        # every authed request; we just read the user_id off it.
        auth = getattr(getattr(request, "state", None), "auth_result", None)
        uid: str = getattr(auth, "user_id", "") or ""
        if cfg.create_supports_user_id_override:
            # Trusted services (currently just ``console``) may forward
            # the real end-user's id in the body so console-proxied
            # creates carry the right owner. Token sources on end-user
            # tokens (including console-proxy tokens that carry the
            # real user's identity at the auth layer) are NOT trusted;
            # only service identities. The deny-by-default keeps a
            # malicious caller from impersonating other users.
            body_uid = body.get("user_id")
            if (
                isinstance(body_uid, str)
                and body_uid
                and auth is not None
                and getattr(auth, "token_source", "") in {"console"}
            ):
                uid = body_uid

        # --- Per-kind pre-create validation ------------------------------
        # Interactive validates ws_id format, kind, parent ownership,
        # attachments+resume_ws combo. Coord 401s on empty uid.
        if cfg.create_validate_request is not None:
            err_validate = await cfg.create_validate_request(request, body, uid, uploaded_files)
            if err_validate is not None:
                return err_validate

        # --- Skill resolution --------------------------------------------
        # Both kinds resolve a body ``skill`` field through
        # ``get_skill_by_name`` to the skill_data dict + the next
        # applied_skill_version. Interactive previously skipped this
        # entirely on resume_ws (the resumed session restores its own
        # skill from config); the resume gate is captured by the
        # interactive validator above (it returns 400 on the
        # attachments+resume combo, but standalone resume_ws + skill
        # is still allowed). To preserve that exact pre-lift skip on
        # interactive, the validator may stash a sentinel — but
        # simplest: re-read resume_ws_id here and skip skill lookup
        # when both kinds see a non-empty resume_ws_id (coord doesn't
        # support resume_ws today; the field is silently ignored).
        # Strip whitespace on the skill name so a caller passing
        # ``"skill": "  "`` is treated identically to ``"skill": ""``
        # (skip skill resolution). Pre-lift coord explicitly stripped
        # via ``(body.get("skill") or "").strip() or None``; pre-lift
        # interactive didn't strip but never received whitespace-only
        # skill names from the web UI. Convergence on the safer
        # behaviour avoids a misleading 400 for an inert payload.
        body_skill_raw = body.get("skill") or ""
        body_skill = body_skill_raw.strip() if isinstance(body_skill_raw, str) else ""
        resume_ws_id_raw = body.get("resume_ws") or ""
        skill_data: dict[str, Any] | None = None
        applied_skill_version = 0

        # --- mgr.create (with skill resolution) -------------------------
        # Skill lookup + version count + ``mgr.create`` all live inside
        # one try/except so any storage failure during skill resolution
        # gets the same correlation_id'd 500 as a ``mgr.create``
        # exception. Pre-lift interactive let storage exceptions
        # propagate to a stack-traced 500; the lifted body keeps the
        # 500 status but redacts the message (operator gets the
        # correlation id; logs carry the full ``exc_info``). The
        # ``RuntimeError`` (capacity) and ``ValueError`` (factory
        # misconfig) branches stay specific to ``mgr.create``: the
        # skill-lookup path doesn't raise either of those.
        if cfg.create_build_kwargs is None:
            # The cfg required a build_kwargs callback for any kind
            # mounting a create handler. Surface the misconfig as 500
            # with a clear log line so the operator sees it instead of
            # a confusing AttributeError.
            log.error("ws.create.misconfigured_no_build_kwargs")
            return JSONResponse(
                {"error": "create handler misconfigured"},
                status_code=500,
            )
        try:
            if body_skill and not (isinstance(resume_ws_id_raw, str) and resume_ws_id_raw):
                from turnstone.core.storage._registry import get_storage as _get_storage

                # Call ``storage.get_prompt_template_by_name`` directly
                # rather than going through
                # ``turnstone.core.memory.get_skill_by_name`` — that
                # helper swallows all storage exceptions into ``None``,
                # which would mask a real outage as the 400 "Skill not
                # found or disabled" branch below. Calling storage
                # directly lets exceptions propagate to the lifted
                # body's correlation_id'd 500 path so operators chasing
                # a "Skill not found" report can distinguish real
                # misses from registry outages.
                _st = _get_storage()
                if _st is None:
                    return JSONResponse({"error": "storage unavailable"}, status_code=503)
                skill_data = await asyncio.to_thread(_st.get_prompt_template_by_name, body_skill)
                if not skill_data or not skill_data.get("enabled", False):
                    return JSONResponse(
                        {"error": f"Skill not found or disabled: {body_skill}"},
                        status_code=400,
                    )
                tid = skill_data.get("template_id")
                if tid:
                    # ``count_skill_versions`` is best-effort: if the
                    # version count call fails (transient storage
                    # blip), default to 1 rather than aborting the
                    # whole create. Persisted skill_version=1 is the
                    # right semantic for the first applied instance
                    # even if the count was unobtainable.
                    try:
                        applied_skill_version = (
                            await asyncio.to_thread(_st.count_skill_versions, str(tid)) + 1
                        )
                    except Exception:
                        log.debug(
                            "ws.create.skill_version_failed skill=%s",
                            body_skill,
                            exc_info=True,
                        )
                        applied_skill_version = 1
            skill_id_resolved = (
                str(skill_data["template_id"])
                if skill_data and skill_data.get("template_id")
                else ""
            )
            kwargs = cfg.create_build_kwargs(
                request, body, uid, skill_data, skill_id_resolved, applied_skill_version
            )
            # Deferred emit — committed below post-attachment-
            # validation. See handler docstring's Ordering invariants.
            ws = await asyncio.to_thread(mgr.create, defer_emit_created=True, **kwargs)
        except RuntimeError as exc:
            # ``SessionManager.create`` documents RuntimeError as
            # "manager at capacity" — translate to 429 (rate-limit /
            # try-later) on both kinds.
            return JSONResponse({"error": str(exc)}, status_code=429)
        except ValueError as exc:
            # Session factory raises ValueError on misconfigured alias
            # (model alias points at a model that no longer exists,
            # etc.). Surface the factory's remediation text as 503 so
            # operators get the actionable message instead of a
            # stack-traced 500.
            return JSONResponse({"error": str(exc)}, status_code=503)
        except Exception:
            # Don't echo the exception text — it can leak internal
            # paths / frame names. Log with a correlation id and
            # return that to the client so support can match a report
            # to the log line.
            correlation_id = secrets.token_hex(4)
            log.warning(
                "ws.create.failed correlation_id=%s",
                correlation_id,
                exc_info=True,
            )
            kind_noun = cfg.audit_action_prefix or "workstream"
            return JSONResponse(
                {
                    "error": (
                        f"failed to create {kind_noun} (internal error). "
                        f"correlation_id={correlation_id}"
                    )
                },
                status_code=500,
            )

        # --- Attachment validation + save + rollback --------------------
        # Validate post-create so ``ws_id`` is bound. Rollback uses
        # ``mgr.discard`` (no ``emit_closed`` because the create was
        # deferred) + ``delete_workstream`` for the storage row. See
        # handler docstring's Ordering invariants for the rationale.
        attachment_ids: list[str] = []
        if uploaded_files:
            saved_ids, save_err = await asyncio.to_thread(
                validate_and_save_uploaded_files, uploaded_files, ws.id, uid
            )
            if save_err is not None:
                from turnstone.core.memory import delete_workstream as _delete_ws

                with contextlib.suppress(Exception):
                    await asyncio.to_thread(mgr.discard, ws.id)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(_delete_ws, ws.id)
                return save_err
            attachment_ids = saved_ids

        # --- Commit the deferred emit_created ----------------------------
        # Synchronous: in-memory non-blocking work on every kind
        # (interactive: no-op stub; coord: dict + ``queue.put_nowait``).
        mgr.commit_create(ws)

        # --- Audit emit --------------------------------------------------
        if audit_emit is not None:
            try:
                audit_emit(request, ws, body, uid)
            except Exception:
                # Mirrors make_close_handler / make_cancel_handler /
                # make_open_handler — audit-write failures shouldn't
                # surface as HTTP 500. Log + continue.
                log.warning(
                    "ws.create.audit_failed ws=%s",
                    ws.id[:8] if ws.id else "",
                    exc_info=True,
                )

        # --- Per-kind post-install ---------------------------------------
        extra_response: dict[str, Any] = {}
        if cfg.create_post_install is not None:
            extra_response = await cfg.create_post_install(
                request,
                ws,
                body,
                uid,
                skill_data,
                applied_skill_version,
                attachment_ids,
            )

        return JSONResponse(
            {
                "ws_id": ws.id,
                "name": ws.name,
                "resumed": bool(extra_response.get("resumed", False)),
                "message_count": int(extra_response.get("message_count", 0)),
                "attachment_ids": attachment_ids,
            }
        )

    return create


def make_list_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``GET {prefix}`` — list workstreams in memory.

    Both kinds share the listing sequence (auth → manager lookup →
    ``mgr.list_all()`` → row serialisation → respond). Per-kind
    divergence captured by:

    - ``cfg.permission_gate`` — coord's ``admin.coordinator`` check;
      interactive ``None`` (auth middleware covers it).
    - ``cfg.manager_lookup`` — already used by every other lifted
      verb.
    - ``cfg.list_resolve_titles`` — interactive's bulk user-alias
      lookup; coord ``None``. Single ``SELECT ... WHERE ws_id IN
      (...)`` resolves every active row's title in one storage
      round-trip (replaces the pre-lift per-row N+1).

    Always-include row shape: ``{ws_id, name, state, kind,
    parent_ws_id, user_id}``. SDK consumers don't branch on kind.

    Behaviour changes vs the pre-lift handlers (documented in
    CHANGELOG):

    - **Top-level response key converges on ``"workstreams"``.**
      Pre-lift coord returned ``{"coordinators": [...]}``; the lifted
      body returns ``{"workstreams": [...]}`` for response-shape
      parity with interactive. Coord SDK / frontend consumers
      branching on ``data.coordinators`` swap to ``data.workstreams``.
    - **Interactive row key renames ``"id"`` → ``"ws_id"``.** Pre-
      lift interactive used the bare ``id`` field while every other
      shared verb on this surface (cancel, open, events, create,
      saved-list) uses ``ws_id``. Convergence eliminates the
      internal inconsistency. Frontend consumers reading
      ``ws.id`` from the active-list response swap to ``ws.ws_id``.
    - **Always-include row fields.** ``user_id`` was coord-only;
      ``kind`` + ``parent_ws_id`` were interactive-only. Both
      kinds now populate all three. ``parent_ws_id`` defaults to
      ``None`` for coord (coordinators have no parent).
    - **Storage / manager-lock work moved off the event loop.**
      ``mgr.list_all()`` acquires the manager mutex; the title
      resolution may dip into storage for the alias lookup. Both
      now run via ``asyncio.to_thread`` (matching coord's pre-
      existing perf-2 pattern from the saved-coordinators review).

    Args:
        cfg: per-kind policy bundle.
    """

    async def list_workstreams_handler(request: Request) -> Response:
        import asyncio

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        # See ``make_approve_handler`` for the cast rationale.
        mgr = cast("SessionManager", mgr_opt)

        # Manager-lock + bulk title resolution off the event loop.
        # ``list_all`` snapshots under the manager lock; the bulk
        # alias lookup hits storage for every row in a single
        # ``SELECT ... WHERE ws_id IN (...)`` (replaces the pre-lift
        # per-row N+1). Running inline would stall every other async
        # handler for the duration of the listing.
        resolve_titles = cfg.list_resolve_titles

        def _build_rows() -> list[dict[str, Any]]:
            wss = mgr.list_all()
            titles: dict[str, str | None] = {}
            if resolve_titles is not None and wss:
                titles = resolve_titles([ws.id for ws in wss])
            rows: list[dict[str, Any]] = []
            for ws in wss:
                title = titles.get(ws.id) or ws.name
                rows.append(
                    {
                        "ws_id": ws.id,
                        "name": title,
                        "state": ws.state.value,
                        "kind": ws.kind,
                        "parent_ws_id": ws.parent_ws_id,
                        "user_id": ws.user_id,
                    }
                )
            return rows

        rows = await asyncio.to_thread(_build_rows)
        return JSONResponse({"workstreams": rows})

    return list_workstreams_handler


def make_saved_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``GET {prefix}/saved`` — list persisted workstreams.

    Both kinds share the storage-backed listing sequence (auth →
    ``list_workstreams_with_history`` filtered by kind → optional
    in-memory exclusion filter → row serialisation → respond).
    Per-kind divergence:

    - ``cfg.permission_gate`` — coord's ``admin.coordinator`` check.
    - ``cfg.list_kind`` — required ``WorkstreamKind`` passed straight
      through to ``list_workstreams_with_history(kind=...)``. The
      handler treats a missing value as a configuration error and
      surfaces 500 with a clear log line — fails loud rather than
      silently filtering for the wrong kind. Distinct from
      ``audit_action_prefix`` (audit-action namespacing) so adding a
      third kind doesn't have to overload the audit prefix as a
      kind classifier.
    - ``cfg.saved_state_filter`` — coord wires ``"closed"`` so only
      explicitly-closed coordinators surface; interactive wires
      ``None`` (any state except the tombstoned ``deleted`` rows the
      storage layer already filters).
    - ``cfg.saved_loaded_lookup`` — coord-only defence-in-depth
      filter that excludes ws_ids currently in the in-memory pool
      (a row can be ``state='closed'`` briefly while the close-emit
      sequence races the in-memory pop). Interactive ``None``.

    Always-include row shape: ``{ws_id, alias, title, name,
    created, updated, message_count}``. Identical between kinds
    pre-lift; the lift just moves the row construction into one
    place.

    Behaviour changes vs the pre-lift handlers:

    - **Top-level response key converges on ``"workstreams"``.**
      Pre-lift coord returned ``{"coordinators": [...]}``; the
      lifted body returns ``{"workstreams": [...]}``. Mirrors the
      active-list convergence.
    - **Interactive's storage call moves to ``asyncio.to_thread``.**
      Pre-lift interactive ran ``list_workstreams_with_history``
      inline — under heavy load the SQL (which includes a
      correlated COUNT subquery) stalled every other async
      handler. Coord already used ``to_thread`` (perf-2 from the
      saved-coordinators review); convergence lifts interactive up.

    Args:
        cfg: per-kind policy bundle.
    """

    async def saved_workstreams_handler(request: Request) -> Response:
        import asyncio

        from turnstone.core.memory import list_workstreams_with_history

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err

        if cfg.list_kind is None:
            # Misconfig: a kind mounted the saved handler without
            # wiring ``cfg.list_kind``. Fail loud instead of silently
            # filtering for the wrong kind — pre-fix the lifted body
            # defaulted to INTERACTIVE on any non-"coordinator"
            # ``audit_action_prefix``, which would have leaked
            # interactive rows on any future kind that forgot the
            # cfg field.
            log.error("ws.saved.misconfigured_no_list_kind")
            return JSONResponse(
                {"error": "saved handler misconfigured"},
                status_code=500,
            )

        rows = await asyncio.to_thread(
            list_workstreams_with_history,
            limit=50,
            kind=cfg.list_kind,
            user_id=None,
            state=cfg.saved_state_filter,
        )

        # Coord-only: exclude ws_ids currently in the warm pool.
        loaded: set[str] = set()
        if cfg.saved_loaded_lookup is not None:
            try:
                loaded = await cfg.saved_loaded_lookup(request)
            except Exception:
                # Defence-in-depth filter — never let a lookup error
                # block the saved list. Log + continue with empty
                # set (worst case: a duplicate row in the saved list
                # for a few seconds during a close-emit race).
                log.debug(
                    "ws.saved.loaded_lookup_failed",
                    exc_info=True,
                )

        # Column order from list_workstreams_with_history is
        # (ws_id, alias, title, name, created, updated, count, node_id) —
        # ``*_extra`` swallows the trailing node_id (and any future
        # columns the SELECT may grow). Keep this comment in sync if
        # the SELECT changes the prefix order.
        result = [
            {
                "ws_id": wid,
                "alias": alias,
                "title": title,
                "name": name,
                "created": created,
                "updated": updated,
                "message_count": count,
            }
            for wid, alias, title, name, created, updated, count, *_extra in rows
            if wid not in loaded
        ]
        return JSONResponse({"workstreams": result})

    return saved_workstreams_handler


def make_history_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``GET {prefix}/{ws_id}/history`` — message history.

    Returns the tail of the workstream's reconstructed conversation as
    OpenAI-like message dicts. Used by coord's page-load handshake (the
    dashboard fetches history once, then SSE handles updates). The lift
    also adds the endpoint to interactive as a feature gain — pre-lift
    interactive only exposed history through the SSE replay on
    ``/events``, so SDK consumers had to subscribe to a stream just to
    read message rows.

    Per-kind divergence captured by:

    - ``cfg.permission_gate`` — coord's ``admin.coordinator`` check;
      interactive ``None``.
    - ``cfg.manager_lookup`` — already used by every other lifted verb.
    - ``cfg.list_kind`` — required for the storage-fallback kind check
      so an interactive ws_id can't read history through the coord
      process and vice versa. Pre-lift coord went through
      :func:`_resolve_coordinator_or_404` for the same isolation; the
      lifted body uses ``cfg.list_kind`` (already wired by both
      production lifespans for the list/saved factories) instead of
      adding a new cfg field. **Required when this handler is mounted**
      — a missing value fails loud (500 + ``log.error``) rather than
      silently leaking cross-kind history through the storage
      fallback. Mirrors :func:`make_saved_handler`'s same gate.
    - ``cfg.not_found_label`` — per-kind 404 wording.

    Pre-lift coord behaviour preserved with one performance lift:
    both the storage-row kind check and the ``load_messages`` call now
    run through ``asyncio.to_thread`` (matched to the rest of the
    lifted verbs' storage offload pattern; pre-lift coord ran them
    inline on the event loop).

    Args:
        cfg: per-kind policy bundle.
    """

    async def history(request: Request) -> Response:
        import asyncio

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err

        # Fail-closed misconfig gate. Without ``cfg.list_kind`` the
        # storage-fallback path below has no way to enforce cross-kind
        # isolation — an interactive ws_id requested through a coord
        # process would silently serve coord history from storage (and
        # vice versa). Mirrors :func:`make_saved_handler`'s same gate
        # for the same reason; a future kind / hand-rolled test cfg
        # that drops the field fails loud instead of leaking rows.
        if cfg.list_kind is None:
            log.error("ws.history.misconfigured_no_list_kind")
            return JSONResponse(
                {"error": "history handler misconfigured"},
                status_code=500,
            )

        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        mgr = cast("SessionManager", mgr_opt)

        ws_id = request.path_params.get("ws_id", "")
        if not ws_id:
            return JSONResponse({"error": "ws_id is required"}, status_code=400)

        # Existence + kind check. The workstream may live only in
        # storage (closed coordinators are still readable via /history
        # without rehydrating; persisted-but-not-loaded interactives
        # are likewise readable). Mirrors the pre-lift coord
        # ``_resolve_coordinator_or_404`` ladder: in-memory mgr.get →
        # storage row + kind check → 404. Falling back to storage
        # without the kind check would leak interactive rows through
        # the coord endpoint (and vice versa) on a process that
        # shares storage with the other kind. ``cfg.list_kind`` is
        # guaranteed non-None by the misconfig gate above.
        storage = getattr(request.app.state, "auth_storage", None)
        if mgr.get(ws_id) is None:
            if storage is None:
                return JSONResponse({"error": cfg.not_found_label}, status_code=404)
            try:
                row = await asyncio.to_thread(storage.get_workstream, ws_id)
            except Exception:
                log.debug("ws.history.lookup_failed ws=%s", ws_id[:8], exc_info=True)
                return JSONResponse({"error": cfg.not_found_label}, status_code=404)
            if row is None or row.get("kind") != cfg.list_kind:
                return JSONResponse({"error": cfg.not_found_label}, status_code=404)

        # Bound the row count. Pre-lift coord clamped to [1, 500].
        try:
            limit = int(request.query_params.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))

        messages: list[dict[str, Any]] = []
        if storage is not None:
            try:
                messages = await asyncio.to_thread(storage.load_messages, ws_id, limit=limit)
            except Exception:
                log.debug("ws.history.load_failed ws=%s", ws_id[:8], exc_info=True)
        return JSONResponse({"ws_id": ws_id, "messages": messages})

    return history


def make_detail_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``GET {prefix}/{ws_id}`` — workstream display fields.

    Returns ``{ws_id, name, state, user_id, kind}`` for the workstream.
    Lazy-rehydrates on miss via ``mgr.open(ws_id)`` so a closed/evicted
    workstream comes back into memory before the response. Mirrors the
    error-handling pattern from :func:`make_open_handler`: ``ValueError``
    from the session factory surfaces as 503 with the factory's
    remediation text; any other rehydrate failure surfaces as a
    correlation-id'd 500 with the per-kind noun in the user-facing
    message.

    Cross-kind isolation is enforced inside ``mgr.open()`` itself —
    it returns ``None`` for missing rows, kind mismatches, and
    tombstoned rows; all surface as 404 with ``cfg.not_found_label``.
    No inline storage check needed (unlike :func:`make_history_handler`)
    because rehydrate is the existence proof.

    Per-kind divergence:

    - ``cfg.permission_gate`` — coord's ``admin.coordinator`` check;
      interactive ``None``.
    - ``cfg.manager_lookup`` — already used by every other lifted verb.
    - ``cfg.not_found_label`` — per-kind 404 wording.
    - ``cfg.audit_action_prefix`` — per-kind noun in the 500 error.

    Pre-lift coord behaviour preserved verbatim. The lift adds the
    endpoint to interactive as a feature gain — pre-lift interactive
    had no HTTP detail endpoint (SDK consumers had to subscribe to
    SSE just to read display fields).

    Args:
        cfg: per-kind policy bundle.
    """

    async def detail(request: Request) -> Response:
        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        mgr = cast("SessionManager", mgr_opt)

        ws_id = request.path_params.get("ws_id", "")
        if not ws_id:
            return JSONResponse({"error": "ws_id is required"}, status_code=400)

        ws = mgr.get(ws_id)
        if ws is None:
            try:
                ws = mgr.open(ws_id)
            except ValueError as exc:
                # Session factory misconfig (e.g. a model alias that no
                # longer resolves). Surface remediation text as 503
                # mirroring :func:`make_open_handler`.
                return JSONResponse({"error": str(exc)}, status_code=503)
            except Exception:
                # Bare ``Exception`` is intentional — see
                # :func:`make_open_handler` for the rationale
                # (``adapter.build_session`` / ``ChatSession.resume``
                # have no documented exception spec).
                import secrets

                correlation_id = secrets.token_hex(4)
                log.warning(
                    "ws.detail.rehydrate_failed correlation_id=%s ws_id=%s",
                    correlation_id,
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )
                kind_noun = cfg.audit_action_prefix or "workstream"
                return JSONResponse(
                    {
                        "error": (
                            f"failed to rehydrate {kind_noun} (internal error). "
                            f"correlation_id={correlation_id}"
                        )
                    },
                    status_code=500,
                )
            if ws is None:
                # ``mgr.open`` returns None for missing rows, kind
                # mismatch, and tombstoned rows — all surface as 404.
                return JSONResponse({"error": cfg.not_found_label}, status_code=404)

        return JSONResponse(
            {
                "ws_id": ws.id,
                "name": ws.name,
                "state": ws.state.value,
                "user_id": ws.user_id,
                "kind": ws.kind,
            }
        )

    return detail


def make_send_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/send`` — message dispatch.

    Reserves any attachment ids the request carries, captures a
    ``send_id`` token for end-to-end tracking, then dispatches via
    :func:`turnstone.core.session_worker.send` (atomic
    spawn-or-enqueue under ``ws._lock``). Both queue-reuse and
    spawn paths reserve so the eventual ``mark_attachments_consumed``
    can match on ``reserved_for_msg_id``.

    Capability flags on ``cfg`` toggle the kind-specific behaviour:

    - ``supports_attachments``: when ``False``, the entire
      attachment-resolution block (reservation, fetch, scope-check)
      short-circuits and any ``attachment_ids`` in the body are
      silently ignored — no reservation, no error. Both kinds wire
      ``True`` post-P1.5; the flag exists so a kind that hasn't
      lit up its UI surface yet can defer.
    - ``spawn_metrics``: when set, fires once on the spawn path with
      ``(request, ui)``. Interactive wires its WebUI per-conversation
      counters here; coord wires ``None``.
    - ``emit_message_queued``: when ``True``, the queue-reuse path
      pushes a ``message_queued`` event onto the listener queue.

    Response shape (both kinds, P1.5 onwards). Every successful
    response carries ``attached_ids`` and ``dropped_attachment_ids``
    (empty lists when no attachments are involved), so SDK
    consumers don't have to branch on whether the request had
    attachments:

    - 200 ``{"status": "ok", "attached_ids", "dropped_attachment_ids"}``
      — fresh worker spawned. ``attached_ids`` is the subset of
      requested attachments that landed (may be a strict subset on
      reservation race losses).
    - 200 ``{"status": "queued", "priority", "msg_id", "attached_ids",
      "dropped_attachment_ids"}`` — reused live worker; queued for
      injection at the next tool-result seam.
    - 200 ``{"status": "queue_full", "attached_ids",
      "dropped_attachment_ids"}`` — live worker's queue at
      capacity; reservations released. Caller should retry. The
      ``attached_ids`` list is always empty here (the dispatch
      didn't take ownership of any reservations).
    - 4xx / 500 — auth / not-found / no-session per the usual
      :class:`SessionEndpointConfig` semantics.
    """
    import asyncio
    import threading
    import uuid

    from turnstone.core import session_worker
    from turnstone.core.session import GenerationCancelled
    from turnstone.core.web_helpers import read_json_or_400

    async def send(request: Request) -> Response:
        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        mgr = cast("SessionManager", mgr_opt)

        body = await read_json_or_400(request)
        if isinstance(body, JSONResponse):
            return body

        ws_id = request.path_params.get("ws_id", "")
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)

        if cfg.tenant_check is not None:
            err_tenant = cfg.tenant_check(request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws = mgr.get(ws_id)
        if ws is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        ui = ws.ui
        if ui is None:
            return JSONResponse({"error": "session UI not available"}, status_code=409)

        # ----- Attachment reservation (atomic reserve-then-dispatch) -----
        send_id = ""
        requested_ids: list[str] = []
        ordered_reserved: list[str] = []
        reserved_set: set[str] = set()
        reserved_ids: list[str] = []
        resolved_atts: list[Any] = []
        attach_user_id = ""

        if cfg.supports_attachments:
            from turnstone.core.attachments import (
                MAX_PENDING_ATTACHMENTS_PER_USER_WS,
                Attachment,
            )
            from turnstone.core.memory import (
                get_attachments as _get_attachments,
            )
            from turnstone.core.memory import (
                get_pending_attachments_with_content as _get_pending_with_content,
            )
            from turnstone.core.memory import (
                reserve_attachments as _reserve,
            )

            if cfg.attachment_owner_resolver is None:
                # Mis-wired config — the resolver is mandatory when
                # attachments are enabled. Fail loudly rather than
                # silently filing under the wrong owner.
                return JSONResponse({"error": "attachment_owner_resolver missing"}, status_code=500)
            attach_user_id, owner_err = cfg.attachment_owner_resolver(request, ws_id, mgr)
            if owner_err is not None:
                return owner_err

            send_id = uuid.uuid4().hex
            raw_ids = body.get("attachment_ids")
            auto_consume_rows: list[dict[str, Any]] = []
            if raw_ids is None:
                # Auto-consume: pull the caller's pending (unreserved)
                # rows in creation order — bytes included so we skip
                # a second fetch below.
                auto_consume_rows = _get_pending_with_content(ws_id, attach_user_id)
                requested_ids = [str(r["attachment_id"]) for r in auto_consume_rows]
            elif isinstance(raw_ids, list) and raw_ids:
                if len(raw_ids) > MAX_PENDING_ATTACHMENTS_PER_USER_WS:
                    return JSONResponse(
                        {
                            "error": (
                                f"Too many attachment_ids "
                                f"(max {MAX_PENDING_ATTACHMENTS_PER_USER_WS})"
                            ),
                            "code": "too_many",
                        },
                        status_code=400,
                    )
                requested_ids = [str(x) for x in raw_ids if x]

            reserved_ids = (
                _reserve(requested_ids, send_id, ws_id, attach_user_id) if requested_ids else []
            )
            reserved_set = set(reserved_ids)
            ordered_reserved = [aid for aid in requested_ids if aid in reserved_set]

            if ordered_reserved:
                if auto_consume_rows and all(
                    str(r["attachment_id"]) in reserved_set for r in auto_consume_rows
                ):
                    rows_by_id = {str(r["attachment_id"]): r for r in auto_consume_rows}
                    # reserved_for_msg_id was None at pre-fetch; patch
                    # in the token so the scope check below admits the
                    # rows we just reserved.
                    for r in rows_by_id.values():
                        r["reserved_for_msg_id"] = send_id
                else:
                    rows = _get_attachments(ordered_reserved)
                    rows_by_id = {str(r["attachment_id"]): r for r in rows}
                for aid in ordered_reserved:
                    row = rows_by_id.get(aid)
                    if not row:
                        continue
                    # Belt-and-braces scope check on top of the reservation.
                    if (
                        row.get("ws_id") != ws_id
                        or row.get("user_id") != attach_user_id
                        or row.get("message_id") is not None
                        or row.get("reserved_for_msg_id") != send_id
                    ):
                        continue
                    content = row.get("content")
                    if not isinstance(content, bytes):
                        continue
                    resolved_atts.append(
                        Attachment(
                            attachment_id=str(row["attachment_id"]),
                            filename=str(row.get("filename") or ""),
                            mime_type=str(row.get("mime_type") or "application/octet-stream"),
                            kind=str(row.get("kind") or ""),
                            content=content,
                        )
                    )

        def _release_reservation_on_fail() -> None:
            """Unreserve if we bail before the dispatcher takes ownership."""
            if reserved_ids:
                from turnstone.core.memory import (
                    unreserve_attachments as _unreserve,
                )

                _unreserve(send_id, ws_id, attach_user_id)

        # If a cancel was just issued, briefly poll for the worker to
        # exit before dispatching — avoids spawning into a stale
        # worker. ``_worker_running`` flips False under ws._lock when
        # the thread reaches its finally block (same gate the
        # dispatcher uses). Async sleep keeps the event loop free.
        if ws._worker_running and ws.session and ws.session._cancel_event.is_set():
            for _ in range(30):  # up to 3s in 100ms steps
                await asyncio.sleep(0.1)
                if not ws._worker_running:
                    break
        if ws.session is None:
            _release_reservation_on_fail()
            return JSONResponse({"error": "No session"}, status_code=500)

        session = ws.session
        # Captured by ``_enqueue`` only when the dispatcher takes the
        # live-worker reuse path. Empty after a fresh-spawn dispatch.
        queue_outcome: dict[str, Any] = {}

        def _enqueue() -> None:
            cleaned, priority, msg_id = session.queue_message(
                message,
                attachment_ids=list(ordered_reserved),
                queue_msg_id=send_id or None,
            )
            queue_outcome["cleaned"] = cleaned
            queue_outcome["priority"] = priority
            queue_outcome["msg_id"] = msg_id

        def _emit_ui(hook_name: str, *args: Any) -> None:
            """Best-effort UI hook dispatch.

            Each call is wrapped in try/except so a failure in one
            hook (e.g. listener-queue full → on_error raises) doesn't
            suppress the others. Mirrors the pre-P1.5
            coord_adapter.send per-hook defense.
            """
            if ui is None:
                return
            method = getattr(ui, hook_name, None)
            if method is None:
                return
            try:
                method(*args)
            except Exception:
                log.debug(
                    "ws.send.ui_hook_failed ws=%s hook=%s",
                    ws.id[:8] if ws.id else "",
                    hook_name,
                    exc_info=True,
                )

        def _run() -> None:
            me = threading.current_thread()
            try:
                kwargs: dict[str, Any] = {}
                if resolved_atts:
                    kwargs["attachments"] = resolved_atts
                if send_id:
                    kwargs["send_id"] = send_id
                session.send(message, **kwargs)
            except GenerationCancelled:
                # Safety net — send() normally handles this internally.
                # If this thread was force-abandoned, ws.worker_thread
                # was set to None — don't emit spurious events.
                _release_reservation_on_fail()
                if ws.worker_thread is me:
                    _emit_ui("on_stream_end")
                    _emit_ui("on_state_change", "idle")
            except Exception as exc:
                # Release the reservation so attachments don't stay
                # soft-locked forever on a worker crash before the
                # consume step. Idempotent: once consume cleared the
                # token, a follow-up unreserve is a no-op.
                _release_reservation_on_fail()
                if ws.worker_thread is me:
                    # ``type(exc).__name__: msg`` carries the exception
                    # class — coord operators triaging worker failures
                    # rely on the class name to disambiguate (model-
                    # alias misconfig vs. tool-policy reject vs. etc.).
                    _emit_ui("on_error", f"{type(exc).__name__}: {exc}")
                    _emit_ui("on_stream_end")
                    _emit_ui("on_state_change", "error")

        ok = session_worker.send(
            ws,
            enqueue=_enqueue,
            run=_run,
            thread_name=f"send-worker-{ws.id[:8]}",
        )
        if not ok:
            # queue.Full or session-disappeared race — surface as
            # queue_full so clients retry rather than 500. Reservations
            # released above; ``attached_ids`` is always empty on this
            # path (the dispatch never took ownership). The empty
            # arrays preserve the response-shape guarantee so SDK
            # consumers don't branch on status.
            _release_reservation_on_fail()
            return JSONResponse(
                {
                    "status": "queue_full",
                    "attached_ids": [],
                    "dropped_attachment_ids": list(requested_ids),
                }
            )

        dropped = [aid for aid in requested_ids if aid not in reserved_set]
        if queue_outcome:
            # Reused a live worker; ``queue_message`` succeeded.
            if cfg.emit_message_queued and hasattr(ui, "_enqueue"):
                ui._enqueue(
                    {
                        "type": "message_queued",
                        "message": queue_outcome["cleaned"],
                        "priority": queue_outcome["priority"],
                        "msg_id": queue_outcome["msg_id"],
                    }
                )
            return JSONResponse(
                {
                    "status": "queued",
                    "priority": queue_outcome["priority"],
                    "msg_id": queue_outcome["msg_id"],
                    "attached_ids": list(ordered_reserved),
                    "dropped_attachment_ids": dropped,
                }
            )

        # Spawned a fresh worker — kind's metrics fire once per turn.
        if cfg.spawn_metrics is not None:
            try:
                cfg.spawn_metrics(request, ui)
            except Exception:
                log.debug(
                    "ws.send.spawn_metrics_failed ws=%s",
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )
        return JSONResponse(
            {
                "status": "ok",
                "attached_ids": list(ordered_reserved),
                "dropped_attachment_ids": dropped,
            }
        )

    return send


def make_attachment_handlers(cfg: SessionEndpointConfig) -> AttachmentHandlers:
    """Lifted bodies for the four per-workstream attachment endpoints.

    Both kinds share the storage layer
    (:mod:`turnstone.core.memory` calls are kind-agnostic) and the
    same per-(``ws_id``, ``user_id``) scope semantics. Differences
    factor into ``cfg.permission_gate`` (auth) and
    ``cfg.attachment_owner_resolver`` (scope + 404 mask).

    ``cfg.supports_attachments`` is checked at registration time —
    callers should only invoke this factory when it's ``True``. The
    factory still returns four working handlers if you call it
    otherwise; they'll just no-op-with-500 when
    ``attachment_owner_resolver`` is unset.
    """
    import uuid

    async def _gate(request: Request) -> JSONResponse | None:
        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        return None

    async def _resolve_owner(request: Request, ws_id: str) -> tuple[str, JSONResponse | None]:
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return "", err503
        mgr = cast("SessionManager", mgr_opt)
        if cfg.attachment_owner_resolver is None:
            return "", JSONResponse({"error": "attachment_owner_resolver missing"}, status_code=500)
        return cfg.attachment_owner_resolver(request, ws_id, mgr)

    async def upload(request: Request) -> Response:
        from turnstone.core.attachments import (
            IMAGE_SIZE_CAP,
            MAX_PENDING_ATTACHMENTS_PER_USER_WS,
            TEXT_DOC_SIZE_CAP,
        )
        from turnstone.core.memory import list_pending_attachments, save_attachment
        from turnstone.core.web_helpers import read_multipart_file_or_400

        # Sniffing helpers stay kind-specific because they're tied to
        # the file-classification policy table; defer to the cfg's
        # owning module via the upload-helper hook.
        if cfg.attachment_helpers is None:
            return JSONResponse({"error": "attachment_helpers missing"}, status_code=500)
        sniff_image = cfg.attachment_helpers.sniff_image_mime
        classify_text = cfg.attachment_helpers.classify_text_attachment
        upload_lock = cfg.attachment_helpers.upload_lock

        err_gate = await _gate(request)
        if err_gate is not None:
            return err_gate

        ws_id = request.path_params.get("ws_id", "")
        if not ws_id:
            return JSONResponse({"error": "ws_id is required"}, status_code=400)

        user_id, err = await _resolve_owner(request, ws_id)
        if err:
            return err

        got = await read_multipart_file_or_400(request, field="file", max_bytes=IMAGE_SIZE_CAP)
        if isinstance(got, JSONResponse):
            return got
        filename, claimed_mime, data = got
        if not data:
            return JSONResponse({"error": "Empty file"}, status_code=400)

        sniffed_image = sniff_image(data)
        if sniffed_image is not None:
            if len(data) > IMAGE_SIZE_CAP:
                return JSONResponse(
                    {
                        "error": (
                            f"Image too large ({len(data):,} bytes); "
                            f"cap is {IMAGE_SIZE_CAP:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )
            kind = "image"
            mime = sniffed_image
        else:
            if len(data) > TEXT_DOC_SIZE_CAP:
                return JSONResponse(
                    {
                        "error": (
                            f"Text document too large ({len(data):,} bytes); "
                            f"cap is {TEXT_DOC_SIZE_CAP:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )
            mime_or_err = classify_text(filename, claimed_mime, data)
            if mime_or_err[0] is None:
                return JSONResponse(
                    {"error": mime_or_err[1], "code": "unsupported"}, status_code=400
                )
            kind = "text"
            mime = mime_or_err[0]

        # Serialize count-check + save per (ws, user) so concurrent
        # uploads can't both pass a check that sees count == cap-1.
        lock = upload_lock(ws_id, user_id)
        with lock:
            if len(list_pending_attachments(ws_id, user_id)) >= MAX_PENDING_ATTACHMENTS_PER_USER_WS:
                return JSONResponse(
                    {
                        "error": (
                            f"Too many pending attachments "
                            f"(max {MAX_PENDING_ATTACHMENTS_PER_USER_WS} pending per workstream)"
                        ),
                        "code": "too_many",
                    },
                    status_code=409,
                )
            attachment_id = uuid.uuid4().hex
            save_attachment(attachment_id, ws_id, user_id, filename, mime, len(data), kind, data)
        return JSONResponse(
            {
                "attachment_id": attachment_id,
                "filename": filename,
                "mime_type": mime,
                "size_bytes": len(data),
                "kind": kind,
            }
        )

    async def list_pending(request: Request) -> Response:
        from turnstone.core.memory import list_pending_attachments

        err_gate = await _gate(request)
        if err_gate is not None:
            return err_gate
        ws_id = request.path_params.get("ws_id", "")
        if not ws_id:
            return JSONResponse({"error": "ws_id is required"}, status_code=400)
        user_id, err = await _resolve_owner(request, ws_id)
        if err:
            return err
        rows = list_pending_attachments(ws_id, user_id)
        return JSONResponse({"attachments": rows})

    async def get_content(request: Request) -> Response:
        from starlette.responses import Response as _Response

        from turnstone.core.memory import get_attachment

        err_gate = await _gate(request)
        if err_gate is not None:
            return err_gate
        ws_id = request.path_params.get("ws_id", "")
        attachment_id = request.path_params.get("attachment_id", "")
        if not ws_id or not attachment_id:
            return JSONResponse({"error": "ws_id and attachment_id are required"}, status_code=400)
        user_id, err = await _resolve_owner(request, ws_id)
        if err:
            return err
        row = get_attachment(attachment_id)
        # Scope on user_id too — id-guessing across users in an
        # unowned workstream would otherwise leak blobs. Mask
        # cross-user / cross-ws as 404 to avoid leaking existence.
        if not row or row.get("ws_id") != ws_id or row.get("user_id") != user_id:
            return JSONResponse({"error": "Not found"}, status_code=404)
        body = row.get("content") or b""
        kind = row.get("kind") or ""
        stored_mime = row.get("mime_type") or "application/octet-stream"
        filename = str(row.get("filename") or "attachment")
        # Force text/plain for text kinds — avoids same-origin HTML/SVG
        # rendering if a user uploaded an HTML-ish text file. Images
        # keep their sniffed MIME (allowlist is strict: png/jpeg/gif/webp).
        response_mime = "text/plain; charset=utf-8" if kind == "text" else stored_mime
        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Cache-Control": "private, no-store",
        }
        return _Response(body, media_type=response_mime, headers=headers)

    async def delete_(request: Request) -> Response:
        from turnstone.core.memory import delete_attachment as _delete

        err_gate = await _gate(request)
        if err_gate is not None:
            return err_gate
        ws_id = request.path_params.get("ws_id", "")
        attachment_id = request.path_params.get("attachment_id", "")
        if not ws_id or not attachment_id:
            return JSONResponse({"error": "ws_id and attachment_id are required"}, status_code=400)
        user_id, err = await _resolve_owner(request, ws_id)
        if err:
            return err
        deleted = _delete(attachment_id, ws_id, user_id)
        if not deleted:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse({"status": "deleted"})

    return AttachmentHandlers(
        upload=upload,
        list=list_pending,
        get_content=get_content,
        delete=delete_,
    )


def make_dequeue_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``DELETE {prefix}/{ws_id}/send`` — cancel a queued message.

    Removes a previously-queued message identified by ``msg_id`` from
    the workstream's pending queue. Returns ``status: removed`` when
    the queue had the entry and ``status: not_found`` otherwise.
    Reservations attached to the dequeued message are released by
    ``ChatSession.dequeue_message`` so attachments can be reused.
    """
    from turnstone.core.web_helpers import read_json_or_400

    async def dequeue(request: Request) -> Response:
        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        mgr = cast("SessionManager", mgr_opt)

        body = await read_json_or_400(request)
        if isinstance(body, JSONResponse):
            return body
        msg_id = body.get("msg_id")
        if not msg_id:
            return JSONResponse({"error": "msg_id required"}, status_code=400)

        # ws_id may come from path (new path-keyed shape) or body
        # (legacy /v1/api/send DELETE under the body-keyed adapter).
        ws_id = request.path_params.get("ws_id", "") or str(body.get("ws_id") or "")
        if cfg.tenant_check is not None:
            err_tenant = cfg.tenant_check(request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws = mgr.get(ws_id)
        if ws is None or ws.ui is None:
            # ``ws.ui is None`` mirrors the pre-P1.5 ``_get_ws`` check —
            # a workstream observed during a partial-construction or
            # close window can have no UI; dequeue would otherwise
            # answer for a session whose listener queues are gone.
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        if ws.session is None:
            return JSONResponse({"error": "No session"}, status_code=400)
        removed = ws.session.dequeue_message(msg_id)
        return JSONResponse({"status": "removed" if removed else "not_found"})

    return dequeue
