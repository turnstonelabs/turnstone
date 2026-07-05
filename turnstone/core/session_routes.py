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
  ``restrict``, ``close_all_children``, ``children``, ``tasks``,
  ``metrics``) that read or mutate state that doesn't exist on
  interactive workstreams.

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
from turnstone.core.session_ui_base import AutoApproveReason

if TYPE_CHECKING:
    from starlette.background import BackgroundTask
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from turnstone.core.attachments import UploadRejection
    from turnstone.core.session_manager import SessionManager
    from turnstone.core.session_ui_base import SessionUIBase
    from turnstone.core.workstream import Workstream, WorkstreamKind

log = get_logger(__name__)


# Cap echoed factory-misconfig messages.  ``ValueError`` from the
# session factory carries an operator-actionable remediation hint
# (``"Unknown model alias: <alias>"`` etc.) that the lifted handlers
# surface as a 503 — but the alias portion is user-controlled on the
# create path (body ``model`` / ``judge_model`` fields) so a raw echo
# reflects arbitrary input back into anything that renders the JSON
# error verbatim.  Length cap + control-char strip keep the message
# actionable for legit alias typos while neutralising hostile payloads.
_FACTORY_MISCONFIG_MAX_LEN = 200


def _safe_factory_misconfig_message(exc: BaseException) -> str:
    """Sanitise a factory-misconfig ``ValueError`` for echo in a 503 body.

    Strips ASCII control characters (``\\x00``-``\\x1f`` + ``\\x7f``)
    and truncates to :data:`_FACTORY_MISCONFIG_MAX_LEN`.  Empty after
    sanitisation falls back to a fixed generic message so a control-
    char-only payload doesn't surface as ``"error": ""``.
    """
    text = str(exc)
    cleaned = "".join(ch for ch in text if ch.isprintable())
    if not cleaned:
        return "session factory misconfigured"
    if len(cleaned) > _FACTORY_MISCONFIG_MAX_LEN:
        # Reserve one codepoint for the ellipsis so the returned string
        # is hard-capped at _FACTORY_MISCONFIG_MAX_LEN total, not
        # MAX_LEN+1.
        cleaned = cleaned[: _FACTORY_MISCONFIG_MAX_LEN - 1] + "…"
    return cleaned


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

    Interactive yields four things on connect: ``connected`` (model +
    skip_permissions), ``status`` (token usage + context %, only when
    ``session._last_usage`` exists), ``history`` (replayed conversation),
    and ``pending_approval`` + cached intent verdicts. Coord yields
    just one: ``pending_approval`` (the rest aren't needed because
    coord's dashboard fetches history via a separate ``/history``
    endpoint and doesn't render the per-tab status bar). Kinds that
    don't need any pre-replay wire ``None`` and the live loop starts
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

    The classification helpers are pure but defined in the kind's owning
    module, so they don't belong on the (frozen)
    :class:`SessionEndpointConfig` directly.  Bundling them on a separate
    dataclass keeps the cfg declarative and lets callers share one helper
    instance across kinds if the policies converge later.  (The old
    ``upload_lock`` hook is gone — uploads now stage into the thread-safe,
    content-addressed per-node buffer, so there's no DB count-check to
    serialize.)
    """

    classify_upload: Callable[
        [str, str, bytes],
        tuple[str | None, str | None, UploadRejection | None],
    ]


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
      Always invoked via ``await asyncio.to_thread(...)`` at handler
      sites: the interactive resolver short-circuits on
      ``mgr.get(ws_id)`` for warm cache but falls through to a
      synchronous storage read (:func:`get_workstream_owner`) on a
      manager-cache miss, so offloading keeps the event loop free
      during cold-cache lookups.
    - ``not_found_label``: the message body for the 404 returned when
      the manager has no such ws_id ("Workstream not found" for
      interactive; "coordinator not found" for coord).
    - ``audit_action_prefix``: the dot-namespaced prefix the kind
      uses for its audit actions ("workstream" → ``workstream.cancel``;
      "coordinator" → ``coordinator.cancel``).

    Capability flags (added with the P1.5 ``send`` body lift):

    - ``supports_attachments``: when ``True``, the lifted ``send``
      handler resolves attachment_ids from the per-node upload buffer
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
    # + pending_approval (with cached intent verdicts). Coord replays
    # just pending_approval (its dashboard fetches history via a
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
    # workstream regardless of state. This is safe because delete is a
    # HARD delete (``session_manager.delete`` -> ``sa.delete(workstreams)``)
    # and no ``state='deleted'`` tombstone is ever written (WorkstreamState
    # has no DELETED member); there is NO storage-side state filter to lean
    # on, so if a soft-delete tombstone is ever introduced this list must
    # add an explicit ``state != 'deleted'`` guard. Coord wires ``"closed"``
    # so only explicitly-closed coordinators surface in the saved-card grid;
    # active / in-flight rows live in the active list.
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
    thumbnail: Handler  # GET    {prefix}/{ws_id}/attachments/{attachment_id}/thumbnail
    delete: Handler  # DELETE {prefix}/{ws_id}/attachments/{attachment_id}


@dataclass(frozen=True)
class SharedSessionVerbHandlers:
    """Bundle of HTTP handler callables for verbs both kinds expose.

    All handlers are optional; ``None`` skips that route. One bundle
    describes either kind — coord omits ``delete``; interactive
    populates every interaction verb post-Stage-2.
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

    # Per-``{ws_id}`` interaction
    send: Handler | None = None  # POST {prefix}/{ws_id}/send
    dequeue: Handler | None = None  # DELETE {prefix}/{ws_id}/send
    approve: Handler | None = None  # POST {prefix}/{ws_id}/approve
    plan: Handler | None = None  # POST {prefix}/{ws_id}/plan
    cancel: Handler | None = None  # POST {prefix}/{ws_id}/cancel
    rewind: Handler | None = None  # POST {prefix}/{ws_id}/rewind
    retry: Handler | None = None  # POST {prefix}/{ws_id}/retry
    events: Handler | None = None  # GET  {prefix}/{ws_id}/events (SSE)
    history: Handler | None = None  # GET  {prefix}/{ws_id}/history
    export: Handler | None = None  # GET  {prefix}/{ws_id}/export

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
    literal subpaths (``saved``, ``new``) before the per-``{ws_id}``
    patterns; per-``{ws_id}/{verb}`` patterns before the bare
    ``{ws_id}`` detail GET.

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

    # --- Lifecycle: create -----------------------------------------------
    if handlers.create is not None:
        routes.append(Route(f"{p}/new", handlers.create, methods=["POST"]))

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
    if handlers.rewind is not None:
        routes.append(Route(f"{p}/{{ws_id}}/rewind", handlers.rewind, methods=["POST"]))
    if handlers.retry is not None:
        routes.append(Route(f"{p}/{{ws_id}}/retry", handlers.retry, methods=["POST"]))
    if handlers.events is not None:
        routes.append(Route(f"{p}/{{ws_id}}/events", handlers.events, methods=["GET"]))
    if handlers.history is not None:
        routes.append(Route(f"{p}/{{ws_id}}/history", handlers.history, methods=["GET"]))
    if handlers.export is not None:
        routes.append(Route(f"{p}/{{ws_id}}/export", handlers.export, methods=["GET"]))

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
                f"{p}/{{ws_id}}/attachments/{{attachment_id}}/thumbnail",
                a.thumbnail,
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


def make_approve_handler(
    cfg: SessionEndpointConfig,
    *,
    accepted_permissions: tuple[str, ...] = (),
) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/approve``.

    Resolves ONE pending approval cycle on the workstream's UI. Both
    kinds expose the same approve / feedback / always / call_id /
    cycle_id body shape and the same cycle-routed
    ``ui.resolve_approval(...)`` mechanic; differences are auth scope,
    manager lookup, and the ``__budget_override__`` filter
    (interactive-only — coord workstreams don't have the
    budget-override pseudo-tool).  With parallel task agents a
    workstream can hold several cycles; a body without a selector
    resolves the oldest.

    ``accepted_permissions`` is OR-checked via :func:`require_any_permission`
    only when ``cfg.permission_gate`` is ``None`` — i.e. for the
    interactive kind, where it IS the primary gate (not a fallback to
    something else). Coord's ``permission_gate`` already takes
    precedence so admin-coordinator users don't also need
    ``tools.approve`` to act on their own coord workstreams. Pass
    ``admin.coordinator`` alongside ``tools.approve`` for endpoints
    reachable by coord sessions spawning interactive children.
    """
    from turnstone.core.auth import require_any_permission
    from turnstone.core.web_helpers import read_json_or_400

    async def approve(request: Request) -> Response:
        import asyncio

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        elif accepted_permissions:
            err = require_any_permission(request, accepted_permissions)
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
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
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
        auto_approve_tools = getattr(ui, "auto_approve_tools", None)
        # Cycle routing — with parallel task agents a workstream can
        # have SEVERAL approval cycles live at once, each its own
        # prompt.  A decision addresses exactly one:
        #   - ``cycle_id`` (new clients) selects it directly;
        #   - ``call_id`` (coord tree rows, channel adapters) selects
        #     the cycle containing that call — and doubles as the
        #     legacy stale-guard: a click on a row whose round was
        #     already replaced 409s instead of silently resolving an
        #     unrelated batch;
        #   - neither (CLI wrappers, old tabs) → the OLDEST live cycle,
        #     matching the order the prompts were issued.
        body_call_id_raw = body.get("call_id", "")
        body_call_id = body_call_id_raw.strip() if isinstance(body_call_id_raw, str) else ""
        body_cycle_id_raw = body.get("cycle_id", "")
        body_cycle_id = body_cycle_id_raw.strip() if isinstance(body_cycle_id_raw, str) else ""
        find_cycle = getattr(ui, "find_approval_cycle", None)
        target_card: dict[str, Any] | None = None
        pinned_cycle_id: str | None = None
        if find_cycle is not None:
            target_card = find_cycle(cycle_id=body_cycle_id or None, call_id=body_call_id or None)
            if target_card is None and (body_cycle_id or body_call_id):
                # Selector given but nothing matched: the round was
                # resolved/replaced after this client rendered it.
                # Report the CURRENT oldest cycle (first entry of
                # ``serialize_pending_approval_details``) so the client
                # can re-render against what the server thinks is live.
                current = find_cycle()
                current_items = (current or {}).get("items") or []
                primary = next(
                    (item.get("call_id", "") for item in current_items if item.get("call_id")),
                    None,
                )
                return JSONResponse(
                    {
                        "error": ("stale call_id" if body_call_id else "stale cycle_id"),
                        "current_call_id": primary,
                        "current_cycle_id": (current or {}).get("cycle_id"),
                    },
                    status_code=409,
                )
            # Pin the resolution to the exact cycle the lookup returned.
            # For selector-less bodies the lookup and the resolve would
            # otherwise EACH independently pick "the oldest" — a cycle
            # resolving in the gap (gate timeout, peer tab, smart
            # approval) silently retargets the resolve at the next
            # cycle while the always-names below were collected from
            # the previous one, whitelisting a batch the operator never
            # looked at.
            pinned_cycle_id = (target_card or {}).get("cycle_id") or None
        else:
            # Legacy/stub UI (tests, external SessionUI impls): fall back
            # to the single-slot view for the always-names read below.
            target_card = getattr(ui, "_pending_approval", None)
            if body_call_id:
                if target_card is None:
                    return JSONResponse(
                        {"error": "no pending approval", "current_call_id": None},
                        status_code=409,
                    )
                legacy_ids = {
                    item.get("call_id", "")
                    for item in target_card.get("items") or []
                    if item.get("call_id")
                }
                if body_call_id not in legacy_ids:
                    primary = next(iter(sorted(legacy_ids)), None)
                    return JSONResponse(
                        {"error": "stale call_id", "current_call_id": primary},
                        status_code=409,
                    )
        # Resolve FIRST, then whitelist: the "Approve + Always" names
        # must describe the cycle that actually resolved.  On the cycle
        # path the resolve is pinned to the lookup's cycle_id, so the
        # only race left is that cycle resolving in the gap — then
        # ``resolved_cycle`` comes back ``None`` and the whitelist below
        # is skipped (approving a card someone else already resolved
        # must not grow the auto-approve set).  ``always`` still rides
        # the ``approval_resolved`` SSE event so peer tabs that didn't
        # click can render the right status pill ("✓ approved · always"
        # vs plain "✓ approved") without a side-channel broadcast.
        try:
            if find_cycle is not None:
                if pinned_cycle_id is not None:
                    resolved_cycle = ui.resolve_approval(
                        approved,
                        feedback,
                        always=always,
                        cycle_id=pinned_cycle_id,
                    )
                elif body_call_id or body_cycle_id:
                    # Lookup matched a card that carries no cycle_id
                    # (custom registrations outside ``approve_tools``):
                    # honor the client's own selector.
                    resolved_cycle = ui.resolve_approval(
                        approved,
                        feedback,
                        always=always,
                        call_id=body_call_id or None,
                        cycle_id=body_cycle_id or None,
                    )
                else:
                    # No selector AND nothing pending at lookup time:
                    # resolve nothing rather than racing a cycle that
                    # registered in the gap — the client can't have
                    # been looking at it.
                    resolved_cycle = None
            else:
                resolved_cycle = ui.resolve_approval(
                    approved,
                    feedback,
                    always=always,
                    call_id=body_call_id or None,
                    cycle_id=body_cycle_id or None,
                )
        except TypeError:
            # Pre-cycle SessionUI impls (external/custom) without the
            # selector kwargs.
            resolved_cycle = ui.resolve_approval(approved, feedback, always=always)
        if (
            always
            and approved
            and target_card
            and auto_approve_tools is not None
            # Cycle-registry UIs: whitelist only when OUR resolve landed
            # on the pinned cycle (non-None return).  Stub/legacy UIs
            # (no registry) keep the unconditional legacy behavior —
            # their resolve's return value carries no cycle contract to
            # gate on.
            and (find_cycle is None or resolved_cycle is not None)
        ):
            tool_names: set[str] = {
                it.get("approval_label", "") or it.get("func_name", "")
                for it in target_card.get("items", [])
                if it.get("needs_approval") and it.get("func_name") and not it.get("error")
            }
            tool_names.discard("")
            # Budget-override is an interactive-only pseudo-tool that
            # must never be added to the auto-approve set — discarding
            # unconditionally is safe (no-op for coord).
            tool_names.discard("__budget_override__")
            if tool_names:
                auto_approve_tools.update(tool_names)
                # Tag the source so /dashboard pills can distinguish
                # an explicit "Approve + Always" click from the
                # skill-template path (which the user may have set
                # up months ago).  Defensive ``getattr`` because the
                # source map landed alongside this fix; pre-fix
                # workstreams would lack it during a hot-deploy.
                source_map = getattr(ui, "_auto_approve_tools_source", None)
                if source_map is not None:
                    for t in tool_names:
                        source_map[t] = AutoApproveReason.ALWAYS
        return JSONResponse({"status": "ok", "cycle_id": resolved_cycle})

    return approve


CloseAuditEmitter = Callable[
    ["Request", str, "Workstream", str],
    None,
]


def make_close_handler(
    cfg: SessionEndpointConfig,
    *,
    audit_emit: CloseAuditEmitter | None = None,
    supports_close_reason: bool = False,
    accepted_permissions: tuple[str, ...] = (),
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
        import asyncio

        from turnstone.core.auth import require_any_permission

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        elif accepted_permissions:
            err = require_any_permission(request, accepted_permissions)
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
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
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


def make_refresh_title_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/refresh-title``.

    Regenerates the workstream title via a background LLM call
    (:meth:`ChatSession.request_title_refresh`). Both kinds share the
    auth → mgr → ws-lookup → request sequence; the session must be live
    in memory (``mgr.get``, not ``open``) since the refresh runs on the
    loaded :class:`ChatSession`. The current display name is passed so
    the generator is steered toward a *different* title on a manual
    refresh.
    """

    async def refresh_title(request: Request) -> Response:
        import asyncio

        from turnstone.core.memory import get_workstream_display_name

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

        if cfg.tenant_check is not None:
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws = mgr.get(ws_id)
        if ws is None or ws.session is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)

        current_title = await asyncio.to_thread(get_workstream_display_name, ws_id) or ""
        ws.session.request_title_refresh(current_title)
        return JSONResponse({"status": "ok"})

    return refresh_title


def make_set_title_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/title``.

    Sets a user-chosen title manually. Stored as the workstream *alias*
    so it outranks the LLM auto-title in the display fallback chain
    (``alias > title > name``). Both kinds share the auth → validate →
    ``set_workstream_alias`` → ``on_rename`` sequence. Returns 409 when
    the name collides with another workstream's alias.

    Behavior matches the pre-lift interactive handler: the alias is set
    against storage regardless of whether the session is loaded (so a
    saved/closed workstream can still be renamed), and the live
    ``on_rename`` broadcast fires only when the session is in memory.
    """

    async def set_title(request: Request) -> Response:
        import asyncio

        from turnstone.core.memory import set_workstream_alias
        from turnstone.core.web_helpers import read_json_or_400

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

        if cfg.tenant_check is not None:
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        # Resolve the workstream BEFORE writing the alias. ``set_workstream_alias``
        # is a global, kind-unscoped UPDATE keyed on ``ws_id`` alone (it returns
        # True even on a 0-row match), so a kind that has no ``tenant_check``
        # storage gate (coord — the in-memory manager is its existence + kind
        # authority) must 404 here, or an operator could rename a workstream this
        # manager doesn't own (e.g. an interactive ws via the coord route) and a
        # bogus id would silently 200. Interactive keeps ``tenant_check`` as its
        # existence gate, so this stays skipped there and a non-loaded
        # saved/closed ws still renames.
        ws = mgr.get(ws_id)
        if cfg.tenant_check is None and ws is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)

        body = await read_json_or_400(request)
        if isinstance(body, JSONResponse):
            return body
        title = str(body.get("title", "")).strip()
        if not title:
            return JSONResponse({"error": "title is required"}, status_code=400)
        title = title[:80]

        if not await asyncio.to_thread(set_workstream_alias, ws_id, title):
            return JSONResponse(
                {"error": "That name is already used by another workstream"},
                status_code=409,
            )

        if ws is not None and ws.session is not None and ws.session.ui is not None:
            ws.session.ui.on_rename(title)
        return JSONResponse({"status": "ok", "title": title})

    return set_title


CancelAuditEmitter = Callable[
    ["Request", str, "Workstream", bool],
    None,
]

# Optional async step run AFTER the workstream's own cancel sequence
# (session.cancel + approval resolution + force/SSE + audit), receiving
# ``(request, ws_id, ws)``.  Coordinator wires this to authorize + prepare the
# cancel fan-out to its spawned children (auto-propagation down the subtree);
# interactive wires ``None`` and the handler is unchanged.  It RETURNS a
# ``BackgroundTask`` (or ``None``) which the handler attaches to its response,
# so the actual per-child fan-out runs AFTER the 200 is sent — the owner's
# cancel is never blocked on child HTTP.  Errors are logged, never surfaced —
# a cascade failure must not fail the owner's own cancel.
PostCancelHook = Callable[["Request", str, "Workstream"], Awaitable["BackgroundTask | None"]]


def make_cancel_handler(
    cfg: SessionEndpointConfig,
    *,
    audit_emit: CancelAuditEmitter | None = None,
    post_cancel: PostCancelHook | None = None,
) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/cancel``.

    Cancels in-flight generation on a workstream. Sets the cooperative
    cancel flag on the session, unblocks any pending approval, and
    (when the request body asks for it) force-abandons a stuck worker
    thread so the UI recovers immediately.

    Both kinds share the cancel sequence (``session.cancel`` →
    ``ui.resolve_approval(False)``).
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
    - Pending approvals are denied via ``resolve_all_approvals`` —
      cancel addresses the workstream, so EVERY live cycle (parallel
      task agents can park several gates at once) wakes with its own
      denied result.  The sweep is a no-op when nothing is pending
      (no stale ``approval_resolved`` broadcast on idle cancels);
      legacy/stub UIs without it fall back to the old single-slot
      ``resolve_approval`` gated on ``_pending_approval``.
    """

    async def cancel(request: Request) -> Response:
        import asyncio

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
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
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
        # nothing's running.  Cancel addresses the WORKSTREAM, so it
        # denies EVERY live approval cycle: with parallel task agents
        # several gate threads can be parked at once and each must wake
        # with its own (denied) result.  ``resolve_all_approvals`` is a
        # no-op with no live cycles (returns 0, no SSE broadcast), so
        # idle cancels stay silent — the same property the old
        # pending-slot gate provided; the recovery semantics for a
        # stuck approval-pending state are preserved because a stuck
        # cycle IS a live cycle.  Legacy/stub UIs without the sweep
        # keep the old single-slot fallback.
        try:
            session.cancel()
        except Exception:
            log.debug("ws.cancel.session_failed ws=%s", ws_id[:8], exc_info=True)
        try:
            if hasattr(ui, "resolve_all_approvals"):
                ui.resolve_all_approvals(False, "Cancelled by user")
            elif (
                hasattr(ui, "resolve_approval")
                and getattr(ui, "_pending_approval", None) is not None
            ):
                ui.resolve_approval(False, "Cancelled by user")
        except Exception:
            log.debug(
                "ws.cancel.resolve_approval_failed ws=%s",
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

        # Propagate the cancel down the subtree (coordinator only).  Runs
        # after the owner's own cancel is fully recorded so a cascade error
        # can't strand the owner half-cancelled.  ``post_cancel`` authorizes
        # and prepares the fan-out, returning a BackgroundTask we attach to the
        # response: the per-child dispatch runs AFTER the 200 is sent, so the
        # owner's cancel never blocks on child HTTP (and we don't drain).
        cancel_background: BackgroundTask | None = None
        if post_cancel is not None:
            try:
                cancel_background = await post_cancel(request, ws_id, ws)
            except Exception:
                log.warning(
                    "ws.cancel.cascade_failed ws=%s",
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )

        return JSONResponse({"status": "ok", "dropped": dropped}, background=cancel_background)

    return cancel


RewindAuditEmitter = Callable[
    ["Request", str, "Workstream", int],
    None,
]
RetryAuditEmitter = Callable[
    ["Request", str, "Workstream"],
    None,
]
# (ws, user_msg) -> None. Re-sends ``user_msg`` on ``ws`` via the kind's
# worker dispatch (driving :func:`turnstone.core.session_worker.send`
# with the kind's own run / enqueue callbacks). The retry handler calls
# it after :meth:`ChatSession.retry` truncates the last turn.
RetryDispatcher = Callable[
    ["Workstream", str],
    None,
]


def make_rewind_handler(
    cfg: SessionEndpointConfig,
    *,
    audit_emit: RewindAuditEmitter | None = None,
    accepted_permissions: tuple[str, ...] = (),
) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/rewind`` (body ``{"turns": N}``).

    Drops the last ``N`` conversation turns via :meth:`ChatSession.rewind`
    (kind-agnostic: mutates ``messages`` + ``_msg_tokens`` + storage, with
    attachment / FTS cleanup riding the app-level cascade inside
    ``delete_messages_after`` — which is exactly why both kinds reuse
    ``rewind()`` rather than bespoke SQL). Both kinds share the auth →
    mgr → ws-lookup → busy-gate → ``rewind`` → ``clear_ui`` → audit
    sequence.

    Unlike :func:`make_close_handler`, the body emits a ``clear_ui`` event
    after the mutation — **always, including a rewind to zero messages**.
    The frontend keys its REST ``/history`` refetch (and any queued
    edit-and-resend) off this signal, not an inline history payload; the
    unconditional emit carries the PR #503 fix (an ``if history:`` guard
    once froze the composer on rewind-to-zero).

    Args:
        cfg: per-kind policy bundle (auth, manager lookup, tenant check,
            error labels). Captured by closure.
        audit_emit: kind's audit emitter; receives
            ``(request, ws_id, ws, turns)``. Wrapped in try/except — an
            audit-write failure logs a warning, never an HTTP 500.
            **Both kinds hardcode the ``conversation.rewind`` action**
            (NOT ``cfg.audit_action_prefix`` — there is deliberately no
            ``coordinator.rewind`` split).
        accepted_permissions: fallback scope check used only when
            ``cfg.permission_gate`` is ``None``. Interactive wires
            ``("conversation.modify",)``; coord leaves it empty and
            relies on its ``admin.coordinator`` ``permission_gate``.
    """

    async def rewind(request: Request) -> Response:
        import asyncio

        from turnstone.core.auth import require_any_permission
        from turnstone.core.web_helpers import read_json_or_400

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        elif accepted_permissions:
            err = require_any_permission(request, accepted_permissions)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        # See ``make_approve_handler`` for the cast rationale.
        mgr = cast("SessionManager", mgr_opt)
        ws_id = request.path_params.get("ws_id", "")

        body = await read_json_or_400(request)
        if isinstance(body, JSONResponse):
            return body
        raw_turns = body.get("turns")
        # ``bool`` is an ``int`` subclass — reject it explicitly so
        # ``{"turns": true}`` can't sneak through as "rewind 1".
        if not isinstance(raw_turns, int) or isinstance(raw_turns, bool) or raw_turns < 1:
            return JSONResponse(
                {"error": "turns must be a positive integer"},
                status_code=400,
            )

        if cfg.tenant_check is not None:
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws = mgr.get(ws_id)
        if ws is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        session = ws.session
        ui = ws.ui
        if session is None or ui is None:
            return JSONResponse({"error": "No session"}, status_code=400)

        # Reject rewind while a generation is in flight — mutating
        # ``messages`` under a running worker corrupts history / cursors.
        # Gate on ``_worker_running`` (not ``worker_thread.is_alive()``)
        # for parity with session_worker.send.
        with ws._lock:
            if ws._worker_running:
                if hasattr(ui, "_enqueue"):
                    ui._enqueue(
                        {"type": "busy_error", "message": "Cannot rewind while processing."}
                    )
                return JSONResponse({"status": "busy"})

        removed = session.rewind(raw_turns)

        if hasattr(ui, "_enqueue"):
            ui._enqueue({"type": "clear_ui"})

        if audit_emit is not None:
            try:
                audit_emit(request, ws_id, ws, raw_turns)
            except Exception:
                log.warning(
                    "ws.rewind.audit_failed ws=%s",
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )

        return JSONResponse({"status": "ok", "removed": removed})

    return rewind


def make_retry_handler(
    cfg: SessionEndpointConfig,
    *,
    dispatch_retry: RetryDispatcher,
    audit_emit: RetryAuditEmitter | None = None,
    accepted_permissions: tuple[str, ...] = (),
) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/retry`` (no body).

    Drops the last assistant response via :meth:`ChatSession.retry` and
    re-sends the last user message for a fresh generation. Shares the
    auth → mgr → ws-lookup → busy-gate → ``retry`` → ``clear_ui`` →
    audit → re-dispatch sequence across kinds.

    The re-send goes through ``dispatch_retry`` (a per-kind closure that
    drives :func:`turnstone.core.session_worker.send` with the kind's own
    ``run`` / ``enqueue`` callbacks) rather than a hand-rolled thread, so
    both kinds converge on the shared worker-dispatch primitive instead
    of open-coding a third copy. A retry issued while busy is rejected up
    front by the busy-gate below; the dispatcher's ``enqueue`` callback
    hard-rejects (rather than queues) so the rare check-then-dispatch
    race can't silently defer the resend behind the in-flight turn.

    ``clear_ui`` fires after ``retry()`` regardless of whether anything
    was dropped (idempotent REST refetch on the frontend), matching the
    pre-lift interactive handler.

    Args:
        cfg: per-kind policy bundle.
        dispatch_retry: ``(ws, user_msg) -> None`` re-send closure.
            Required — retry is meaningless without it.
        audit_emit: kind's audit emitter; receives ``(request, ws_id,
            ws)``. **Both kinds hardcode the ``conversation.retry``
            action.** Wrapped in try/except.
        accepted_permissions: fallback scope check used only when
            ``cfg.permission_gate`` is ``None`` (interactive wires
            ``("conversation.modify",)``).
    """

    async def retry(request: Request) -> Response:
        import asyncio

        from turnstone.core.auth import require_any_permission

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        elif accepted_permissions:
            err = require_any_permission(request, accepted_permissions)
            if err is not None:
                return err
        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        mgr = cast("SessionManager", mgr_opt)
        ws_id = request.path_params.get("ws_id", "")

        if cfg.tenant_check is not None:
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws = mgr.get(ws_id)
        if ws is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        session = ws.session
        ui = ws.ui
        if session is None or ui is None:
            return JSONResponse({"error": "No session"}, status_code=400)

        with ws._lock:
            if ws._worker_running:
                if hasattr(ui, "_enqueue"):
                    ui._enqueue({"type": "busy_error", "message": "Cannot retry while processing."})
                return JSONResponse({"status": "busy"})

        # A retry is a fresh turn initiated by the authenticated caller —
        # rebind per-user MCP credential resolution to them before the
        # re-send dispatches (the per-kind ``dispatch_retry`` closure
        # calls ``send()`` without identity kwargs). getattr-guarded so
        # per-kind session stubs without the method keep working.
        from turnstone.core.web_helpers import auth_user_id

        acting_uid = auth_user_id(request)
        bind_acting = getattr(session, "bind_acting_user", None)
        if acting_uid and callable(bind_acting):
            bind_acting(acting_uid)

        retry_msg = session.retry()

        if hasattr(ui, "_enqueue"):
            ui._enqueue({"type": "clear_ui"})

        if audit_emit is not None:
            try:
                audit_emit(request, ws_id, ws)
            except Exception:
                log.warning(
                    "ws.retry.audit_failed ws=%s",
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )

        retried = retry_msg is not None
        if retry_msg is not None:
            dispatch_retry(ws, retry_msg)

        return JSONResponse({"status": "ok", "retried": retried})

    return retry


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
            log.warning("ws.open.factory_misconfig ws_id=%s exc=%r", ws_id[:8], exc)
            return JSONResponse({"error": _safe_factory_misconfig_message(exc)}, status_code=503)
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
                # Off-loop: interactive's post_load does blocking
                # storage I/O (a workstream display-name lookup) that
                # would otherwise stall the event loop on every
                # workstream open.
                await asyncio.to_thread(cfg.open_post_load, request, ws)
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
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
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

        # ``Last-Event-ID`` resume: native EventSource auto-reconnect
        # sends the header; the manual-reconnect path (which uses
        # ``new EventSource(url)`` and can't set custom headers) sends
        # ``?last_event_id=N``.  Accept both; malformed values fall
        # back to fresh-connect semantics so a broken intermediary
        # can't break replay for a client that genuinely lost no
        # events.
        last_event_id_raw = request.headers.get("Last-Event-ID") or request.query_params.get(
            "last_event_id"
        )
        last_event_id: int | None
        try:
            last_event_id = int(last_event_id_raw) if last_event_id_raw else None
        except (TypeError, ValueError):
            last_event_id = None

        # Three replay shapes:
        #   - ``last_event_id is None`` → ``"fresh"`` (today's behaviour):
        #     replay_cb + state_change + in_progress_snapshot + live.
        #   - ``last_event_id`` + buffer covers gap → ``"replay_ok"``:
        #     emit buffered events past the id, SKIP replay_cb /
        #     state_change / in_progress_snapshot (the buffered stream
        #     already contains them), then live drain.
        #   - ``last_event_id`` + buffer too short → ``"truncated"``:
        #     emit a ``replay_truncated`` envelope so the client knows
        #     it lost live ticks, then fall through to the fresh
        #     replay (history / state_change / in_progress_snapshot)
        #     as the recovery floor.
        # The placeholder-UI guard above (which 409s when
        # ``_register_listener`` is missing) already proves that
        # ``ui`` is a ``SessionUIBase`` subclass, so the cast is
        # tightening the type, not weakening it.
        ui_base = cast("SessionUIBase", ui)
        replay_status: str
        replay_events: list[dict[str, Any]] = []
        lost_count = 0
        earliest_available_id = 0
        in_progress_snap: dict[str, Any]
        snap_seq: int = 0
        if last_event_id is None:
            replay_status = "fresh"
            client_queue, in_progress_snap = ui_base.register_listener_with_in_progress_snapshot()
            snap_seq = in_progress_snap["seq"]
        else:
            (
                client_queue,
                replay_events,
                replay_status,
                lost_count,
                earliest_available_id,
                snapshot,
            ) = ui_base.register_listener_with_replay(last_event_id)
            if replay_status == "truncated":
                # Truncated → emit ``replay_truncated`` envelope, then
                # the snapshot is the recovery floor.  ``snap_seq``
                # MUST come from the snapshot capture (not 0), because
                # writers can race between
                # ``register_listener_with_replay`` returning and our
                # first live-drain read: any token event landing in
                # the listener queue between registration and the
                # captured ``_event_id`` is ALSO covered by the
                # snapshot's content/reasoning text, and would
                # double-render without the ``_seq <= snap_seq`` dedup
                # filter on the live path.  The helper captured both
                # under the same nested-lock acquire, so this
                # ``snap_seq`` is exactly the high-water mark
                # corresponding to the snapshot text.
                in_progress_snap = snapshot
                snap_seq = snapshot["seq"]
            else:
                # ``replay_ok``: the buffered events ARE the partial
                # token stream (no separate snapshot needed); the
                # synthetic snapshot/state_change/history emission is
                # skipped by the events handler.  No live-dedup
                # filtering required because the buffered events
                # themselves are the cutoff — anything past the last
                # replayed event id is genuinely new live traffic
                # that lands in the listener queue after the buffer
                # snapshot was taken (atomic-against-writers under
                # the registration's nested locks).
                in_progress_snap = {"content": "", "reasoning": "", "seq": 0}

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
            import random

            _metrics.record_sse_connect()
            loop = asyncio.get_running_loop()

            def _format_event(event: dict[str, Any]) -> dict[str, str]:
                """Strip internal plumbing fields, attach SSE ``id:`` if present.

                Shallow-copies the dict before any mutation because
                ``_enqueue`` puts ONE reference into every listener
                queue (no per-listener copy) and stores the SAME
                reference in the per-ws ring buffer.  Without a
                shallow copy here, listener A's pop of ``_event_id``
                would silently strip the field from listener B's view
                AND from the buffer's view, breaking the replay
                guarantee for a later-arriving subscriber.
                """
                ev_copy = dict(event)
                eid = ev_copy.pop("_event_id", None)
                # Strip ``_seq`` here too — it's internal plumbing for
                # the snapshot dedup; clients never need to see it on
                # the wire.  The fresh-path live drain filters on
                # ``_seq`` BEFORE calling this helper.
                ev_copy.pop("_seq", None)
                out: dict[str, str] = {"data": json.dumps(ev_copy)}
                if eid is not None:
                    out["id"] = str(eid)
                return out

            try:
                # Per-stream reconnect interval jitter.  Without this,
                # all panes on a workstream disconnect together and
                # reconnect in lockstep at the same backoff intervals
                # (EventSource's default ~3 s with no jitter, or
                # whatever ``retry:`` value the server last sent).
                # 2.5 – 4.5 s spread keeps the average reconnect rate
                # below today's ping cadence while staggering peaks.
                yield {"retry": random.randint(2500, 4500)}

                if replay_status == "replay_ok":
                    # Buffered events already cover everything since
                    # the client's ``Last-Event-ID`` — skip the
                    # synthetic replay (history / state_change /
                    # in_progress_snapshot) which would otherwise
                    # double-render content the buffer already
                    # contains.  Yield buffered events in order with
                    # their ``_event_id`` as SSE ``id:`` so a
                    # disconnect mid-replay resumes from the latest
                    # buffered id, not the original ``last_event_id``.
                    for ev in replay_events:
                        yield _format_event(ev)
                else:
                    # ``fresh`` or ``truncated`` — both run the
                    # synthetic replay (kind-specific replay_cb +
                    # state_change + in_progress_snapshot).  On
                    # ``truncated`` we emit the explicit envelope
                    # first so the client knows the buffer couldn't
                    # cover the gap and treats the snapshot below as
                    # the recovery floor.
                    if replay_status == "truncated":
                        yield {
                            "data": json.dumps(
                                {
                                    "type": "replay_truncated",
                                    "ws_id": ws_id,
                                    "lost_count": lost_count,
                                    "earliest_available_id": earliest_available_id,
                                }
                            )
                        }

                    # Replay phase — stream the kind-specific initial
                    # payload one event at a time so the client sees
                    # the first byte immediately.  Pre-building into
                    # a list would block time-to-first-byte until the
                    # entire replay materialized AND let the listener
                    # queue accumulate (potentially over its 500-slot
                    # cap on a chatty mid-generation workstream)
                    # while replay was being built.  Synthetic
                    # events carry no ``_event_id`` — they intentionally
                    # don't advance the client's ``lastEventId``, so
                    # a mid-replay disconnect reconnects with the
                    # last BUFFERED id (or none on truly-fresh
                    # connect), which is what the server can replay.
                    if replay_cb is not None:
                        try:
                            for ev in replay_cb(ws, ui, request):
                                yield {"data": json.dumps(ev)}
                        except Exception:
                            # Replay is observational — never let a
                            # snapshot bug block the live stream.
                            log.debug(
                                "ws.events.replay_failed ws=%s",
                                ws_id[:8],
                                exc_info=True,
                            )
                    # Refresh-resume tail: emit the current
                    # workstream state and the in-progress snapshot.
                    # Both are best-effort — a ws.state read failure
                    # or empty buffers just yields nothing extra.
                    try:
                        cur_state = getattr(ws.state, "value", None)
                        if isinstance(cur_state, str) and cur_state:
                            state_evt: dict[str, Any] = {
                                "type": "state_change",
                                "state": cur_state,
                                "ws_id": ws_id,
                            }
                            # A client connecting mid-turn learns who holds it,
                            # so it can gate its send button (matches the live
                            # state_change emitted from server.WebUI).
                            sess = getattr(ws, "session", None)
                            acting = (
                                getattr(sess, "_acting_user_id", "")
                                or getattr(sess, "_user_id", "")
                                if sess is not None
                                else ""
                            )
                            if acting:
                                state_evt["acting_user_id"] = acting
                            yield {"data": json.dumps(state_evt)}
                    except Exception:
                        log.debug(
                            "ws.events.state_change_replay_failed ws=%s",
                            ws_id[:8],
                            exc_info=True,
                        )
                    if in_progress_snap["content"] or in_progress_snap["reasoning"]:
                        yield {
                            "data": json.dumps(
                                {
                                    "type": "in_progress_snapshot",
                                    "content": in_progress_snap["content"],
                                    "reasoning": in_progress_snap["reasoning"],
                                    "ws_id": ws_id,
                                }
                            )
                        }
                    # Surface the persisted ``last_error`` so a fresh
                    # connect to a workstream sitting in the error state
                    # shows WHY it failed (the ``error`` text bubble), not
                    # just the bare error state + retry affordance.
                    # ``on_error`` is never persisted as a message, so
                    # ``/history`` can't rebuild it — the ``last_error``
                    # config row (set by ``_record_fatal_error``, cleared
                    # on recovery) is the only durable source.  Gated on
                    # the error state so a healthy ws skips the storage
                    # read, and confined to this fresh/truncated path —
                    # the ``replay_ok`` branch's ring buffer already
                    # carries the original ``error`` event.
                    try:
                        if getattr(ws.state, "value", None) == "error":
                            from turnstone.core.memory import load_last_error

                            last_err = await asyncio.to_thread(load_last_error, ws_id)
                            if last_err:
                                # Carry the SSE ``id:`` (registration-time
                                # buffer position) so the client's
                                # ``lastEventId`` advances past this surface.
                                # Unlike the idempotent ``state_change`` /
                                # ``in_progress_snapshot`` above, the client
                                # APPENDS this ``error`` bubble (non-idempotent),
                                # and a terminal-errored idle ws emits no live
                                # event to set a cursor — so without an ``id:``
                                # a native EventSource reconnect would send no
                                # ``Last-Event-ID``, re-run this fresh path, and
                                # append a DUPLICATE bubble on every reconnect
                                # cycle.  With it, the reconnect resumes via
                                # ``replay_ok`` (nothing buffered past
                                # ``snap_seq`` on an idle ws) and skips this
                                # surface.
                                yield {
                                    "id": str(snap_seq),
                                    "data": json.dumps(
                                        {
                                            "type": "error",
                                            "message": last_err,
                                            "ws_id": ws_id,
                                        }
                                    ),
                                }
                    except Exception:
                        log.debug(
                            "ws.events.last_error_replay_failed ws=%s",
                            ws_id[:8],
                            exc_info=True,
                        )
                # Live phase — drain the per-UI listener queue
                # until either the workstream closes or the client
                # disconnects. 5s poll matches pre-lift interactive
                # (the ``is_disconnected`` probe between polls covers
                # cancel-detection latency the timeout would
                # otherwise gate; shortening to 1s 5x'd the wakeup
                # rate without any client-observable benefit).
                #
                # ``_seq`` filter: ``on_content_token`` /
                # ``on_reasoning_token`` tag each emit with the
                # per-ws event counter.  On the ``fresh`` path,
                # events whose seq is already covered by the
                # snapshot we just yielded get dropped to avoid
                # double-rendering.  On ``replay_ok`` / ``truncated``
                # paths, ``snap_seq`` is 0 so no live event is
                # filtered — the replay buffer (or replay_truncated
                # envelope) has already established the cutoff.
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
                    seq = event.get("_seq")
                    if seq is not None and seq <= snap_seq:
                        continue
                    yield _format_event(event)
            finally:
                _metrics.record_sse_disconnect()
                unregister(client_queue)

        return EventSourceResponse(event_generator(), ping=5)

    return events


def make_create_handler(
    cfg: SessionEndpointConfig,
    *,
    audit_emit: CreateAuditEmitter | None = None,
    accepted_permissions: tuple[str, ...] = (),
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
      ``initial_message``, the uploads are resolved from the buffer onto
      the dispatched first turn via ``CoordinatorAdapter.send`` (which
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
        from turnstone.core.auth import require_any_permission
        from turnstone.core.web_helpers import (
            read_json_or_400,
            read_multipart_create_or_400,
        )

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        elif accepted_permissions:
            err = require_any_permission(request, accepted_permissions)
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
            # Multipart cap: up to 10 files × the image cap, plus slack
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

            # --- Persona resolution (resolve ONCE, stamp forever) --------
            # Same gate shape as the skill lookup above: an explicit name
            # must exist, be enabled, and support this kind (400
            # otherwise); an empty name resolves to the kind's default
            # persona.  A pre-seed database (no default persona) creates
            # unstamped — legacy behavior, byte-identical to the
            # engineer/orchestrator defaults.  Resume skips resolution:
            # the resumed session restores its own stamp from config.
            body_persona_raw = body.get("persona") or ""
            # [:64] matches the console proxy's cap: real slugs fit, and an
            # oversized value must not reach the storage lookup or reflect
            # into the 400 error text.
            body_persona = (body_persona_raw.strip() if isinstance(body_persona_raw, str) else "")[
                :64
            ]
            persona_snapshot = None
            if isinstance(resume_ws_id_raw, str) and resume_ws_id_raw:
                # Fork-resume adopts the SOURCE workstream's stamp, resolved
                # pre-construction so all four levers (including the
                # construction-time MCP gate) apply to the fork.  The four
                # levers a fork runs under must be the ones its conversation
                # was authored under — never a fresh default.  A corrupt
                # stamp is a loud 400, mirroring the rehydrate contract; an
                # unstamped (legacy) source forks unstamped.
                from turnstone.core.memory import resolve_workstream
                from turnstone.core.personas import snapshot_from_config
                from turnstone.core.storage._registry import get_storage as _get_storage

                _st = _get_storage()
                resume_target = await asyncio.to_thread(resolve_workstream, resume_ws_id_raw)
                if _st is not None and resume_target:
                    try:
                        persona_snapshot = snapshot_from_config(
                            await asyncio.to_thread(_st.load_workstream_config, resume_target) or {}
                        )
                    except ValueError as exc:
                        return JSONResponse(
                            {"error": f"cannot fork {resume_ws_id_raw}: {exc}"},
                            status_code=400,
                        )
            else:
                from turnstone.core.personas import (
                    resolve_persona_for_kind,
                    snapshot_from_persona,
                )
                from turnstone.core.storage._registry import get_storage as _get_storage

                persona_row: dict[str, Any] | None = None
                if body_persona:
                    _st = _get_storage()
                    if _st is None:
                        return JSONResponse({"error": "storage unavailable"}, status_code=503)
                    persona_row, persona_err = await asyncio.to_thread(
                        resolve_persona_for_kind, _st, body_persona, mgr.kind.value
                    )
                    if persona_err:
                        return JSONResponse({"error": persona_err}, status_code=400)
                else:
                    # No explicit persona: stamp the kind's default. A clean
                    # ``None`` (no default configured — pre-seed DB) creates
                    # unstamped legacy, but a FAILED lookup must not: the
                    # operator may have promoted a restricted persona to
                    # default, and degrading to the stock envelope on a
                    # storage blip would silently widen it.
                    _st = _get_storage()
                    if _st is not None:
                        try:
                            persona_row = await asyncio.to_thread(
                                _st.get_default_persona, mgr.kind.value
                            )
                        except Exception:
                            log.warning("ws.create.default_persona_lookup_failed", exc_info=True)
                            return JSONResponse(
                                {"error": "persona resolution unavailable"},
                                status_code=503,
                            )
                if persona_row is not None:
                    persona_snapshot = snapshot_from_persona(persona_row)

            kwargs = cfg.create_build_kwargs(
                request, body, uid, skill_data, skill_id_resolved, applied_skill_version
            )
            if persona_snapshot is not None:
                # ``persona`` is SessionManager.create's explicit param
                # (Workstream attr + workstreams row); the snapshot rides
                # **extra_session_kwargs into the session factory.
                kwargs["persona"] = persona_snapshot.name
                kwargs["persona_snapshot"] = persona_snapshot
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
            # stack-traced 500. Sanitiser caps + scrubs the echoed
            # text since the alias is user-controlled on the create
            # path (body ``model`` / ``judge_model``).
            log.warning("ws.create.factory_misconfig exc=%r", exc)
            return JSONResponse({"error": _safe_factory_misconfig_message(exc)}, status_code=503)
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
            from turnstone.core.auth import WorkstreamProjectVisibility

            visibility = WorkstreamProjectVisibility.for_request(request)
            wss = mgr.list_all()
            titles: dict[str, str | None] = {}
            if resolve_titles is not None and wss:
                titles = resolve_titles([ws.id for ws in wss])
            rows: list[dict[str, Any]] = []
            for ws in wss:
                raw_pid = getattr(ws, "project_id", "")
                project_id = raw_pid if isinstance(raw_pid, str) else ""
                # Same guarded read as project_id — test doubles and older
                # node payloads may lack the attribute.
                raw_persona = getattr(ws, "persona", "")
                persona = raw_persona if isinstance(raw_persona, str) else ""
                # Private-project tenancy — drop rows the requester may
                # not see (same predicate as the saved list).
                if not visibility.ws_visible(project_id, ws_owner=ws.user_id or ""):
                    continue
                title = titles.get(ws.id) or ws.name
                rows.append(
                    {
                        "ws_id": ws.id,
                        "name": title,
                        "state": ws.state.value,
                        "kind": ws.kind,
                        "parent_ws_id": ws.parent_ws_id,
                        "user_id": ws.user_id,
                        "project_id": project_id or None,
                        "persona": persona or None,
                    }
                )
            return rows

        rows = await asyncio.to_thread(_build_rows)
        return JSONResponse({"workstreams": rows})

    return list_workstreams_handler


async def _collect_saved_rows(
    cfg: SessionEndpointConfig,
    request: Request,
) -> list[dict[str, Any]]:
    """Query + exclude-loaded + row-build for one kind's saved list.

    The shared inner body of :func:`make_saved_handler` (and one of the
    N bodies :func:`make_unified_saved_handler` fans over): runs the
    storage query filtered by ``cfg.list_kind`` / ``cfg.saved_state_filter``,
    drops any ws_id reported by ``cfg.saved_loaded_lookup`` (coord-only
    warm-pool exclusion), and serialises each surviving row to the
    saved-card dict shape.

    Caller-owned (NOT done here): ``cfg.permission_gate`` and the
    ``cfg.list_kind is None`` misconfig guard — both belong to the
    handler wrapper so the unified handler can gate once and 500 per
    cfg before fanning out. ``cfg.list_kind`` is therefore assumed
    non-``None`` on entry.
    """
    import asyncio

    from turnstone.core.auth import WorkstreamProjectVisibility
    from turnstone.core.memory import list_workstreams_with_history

    visibility = WorkstreamProjectVisibility.for_request(request)

    def _fetch_visible_rows() -> list[Any]:
        """Page through storage until 50 visible rows (or exhaustion).

        The visibility filter runs post-SQL, so a plain LIMIT-then-filter
        would silently shrink the window whenever recently-updated rows
        belong to private projects the caller can't see — their own rows
        at position 51+ would never surface. Paging with OFFSET restores
        the 'top-50 most-recent VISIBLE' contract. Runs entirely in the
        worker thread: both the query and the per-row project lookups
        are storage I/O. Bounded at 20 pages (1000 rows scanned) as a
        runaway guard; hitting it is logged, not silent.
        """
        visible: list[Any] = []
        offset = 0
        page = 50
        max_pages = 20
        for _ in range(max_pages):
            batch = list_workstreams_with_history(
                limit=page,
                kind=cfg.list_kind,
                user_id=None,
                state=cfg.saved_state_filter,
                offset=offset,
            )
            for row in batch:
                # project_id / owner are the SELECT tail — see the column
                # order comment below.
                if visibility.ws_visible(row[15], ws_owner=row[16] or ""):
                    visible.append(row)
                    if len(visible) >= 50:
                        return visible
            if len(batch) < page:
                return visible
            offset += page
        log.info(
            "ws.saved.visibility_scan_capped kind=%s scanned=%d visible=%d",
            cfg.list_kind,
            max_pages * page,
            len(visible),
        )
        return visible

    rows = await asyncio.to_thread(_fetch_visible_rows)

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

    # Column order from list_workstreams_with_history (keep in sync with
    # the storage SELECT): ws_id, alias, title, name, created, updated,
    # message_count, node_id, state, kind, model_alias, launch_skill,
    # child_count, context_tokens, context_window, project_id, owner,
    # persona.
    # The occupancy ratio is derived here (Python float division) rather
    # than in SQL so the NULL / zero-window cases stay obvious and
    # identical across backends.  context_window is NULL for model
    # aliases absent from model_definitions (e.g. config.toml-only
    # models), so context_ratio degrades to 0.0 there rather than
    # reporting a bogus occupancy.
    result: list[dict[str, Any]] = []
    for row in rows:
        (
            wid,
            alias,
            title,
            name,
            created,
            updated,
            count,
            node_id,
            state,
            kind,
            model_alias,
            launch_skill,
            child_count,
            context_tokens,
            context_window,
            project_id,
            owner,
            persona,
        ) = row
        if wid in loaded:
            continue
        ctx_tokens = context_tokens or 0
        context_ratio = (
            round(ctx_tokens / context_window, 3) if ctx_tokens and context_window else 0.0
        )
        result.append(
            {
                "ws_id": wid,
                "alias": alias,
                "title": title,
                "name": name,
                "created": created,
                "updated": updated,
                "message_count": count,
                "node_id": node_id or "",
                "state": state,
                "kind": kind,
                "model_alias": model_alias or None,
                "launch_skill": launch_skill or None,
                "child_count": child_count or 0,
                "context_tokens": ctx_tokens,
                "context_ratio": context_ratio,
                "project_id": project_id or None,
                "persona": persona or None,
            }
        )
    return result


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

        result = await _collect_saved_rows(cfg, request)
        return JSONResponse({"workstreams": result})

    return saved_workstreams_handler


def _saved_updated_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    """Descending-``updated`` sort key for the merged saved list.

    Used with ``sorted(..., reverse=True)``. Returns ``(1, str(updated))``
    for rows carrying an ``updated`` value and ``(0, "")`` for rows
    missing it — so under ``reverse=True`` real rows sort newest-first
    and ``updated``-less rows fall to the tail (the presence flag differs
    so the string element of a present row is never compared against the
    absent placeholder). ``updated`` is coerced to ``str`` purely as a
    crash-proof comparator: each storage backend yields a single
    homogeneous timestamp type (ISO string on sqlite, ``datetime`` on
    postgres) whose lexical order matches chronological order, and
    ``list_workstreams_with_history`` already returns each kind
    ``ORDER BY updated DESC``, so this only re-interleaves two
    already-sorted same-type runs — never an int-vs-string compare.
    """
    updated = row.get("updated")
    if updated is None:
        return (0, "")
    return (1, str(updated))


def make_unified_saved_handler(
    cfgs: list[SessionEndpointConfig],
    permission_gate: PermissionGate | None = None,
) -> Handler:
    """Saved-list handler that spans multiple kinds in one response.

    The console's L-shell dashboard wants ONE saved list covering both
    coordinator and interactive sessions. Storage is shared across kinds,
    so this fans :func:`_collect_saved_rows` over each ``cfg`` (preserving
    every kind's own ``list_kind`` / ``saved_state_filter`` /
    ``saved_loaded_lookup`` semantics — coord keeps its ``state='closed'``
    + warm-pool exclusion, interactive keeps its all-non-deleted listing),
    concatenates the rows, and re-sorts the union by ``updated`` descending
    (``updated``-less rows last).

    Permission: a SINGLE ``permission_gate`` runs once up front (the
    operator gate the console already applies to its coordinator saved
    list). Per-``cfg`` ``permission_gate`` values are deliberately NOT
    consulted here — the union is operator-gated as a whole, and the
    operator already has cluster-wide visibility into every kind, so the
    merge exposes nothing the per-kind lists didn't.

    Each ``cfg`` must wire ``list_kind`` (a missing value is a mount-time
    misconfiguration); the handler 500s loud rather than silently
    filtering for the wrong kind, matching :func:`make_saved_handler`.

    Args:
        cfgs: per-kind policy bundles to merge, in any order (the response
            is re-sorted by ``updated`` regardless of cfg order).
        permission_gate: single gate applied to the whole list. ``None``
            relies on upstream auth middleware only.
    """

    async def unified_saved_handler(request: Request) -> Response:
        if permission_gate is not None:
            err = permission_gate(request)
            if err is not None:
                return err

        # Fail loud BEFORE any query (same contract as the single-kind
        # handler): a cfg mounted into the union without a kind would
        # otherwise filter for the wrong (or all) kinds.
        for cfg in cfgs:
            if cfg.list_kind is None:
                log.error("ws.saved.unified.misconfigured_no_list_kind")
                return JSONResponse(
                    {"error": "saved handler misconfigured"},
                    status_code=500,
                )

        # The per-kind collections are independent (shared store, no data
        # dependency), so overlap their DB round-trips instead of summing them.
        import asyncio

        parts = await asyncio.gather(*(_collect_saved_rows(cfg, request) for cfg in cfgs))
        merged: list[dict[str, Any]] = []
        for part in parts:
            merged.extend(part)

        merged.sort(key=_saved_updated_sort_key, reverse=True)
        return JSONResponse({"workstreams": merged})

    return unified_saved_handler


def _resume_cursor_and_trim(
    messages: list[dict[str, Any]],
    ui: Any,
    awaiting_approval: bool,
) -> tuple[list[dict[str, Any]], int | None]:
    """Decide the fresh-connect resume cursor and trim the in-flight turn.

    Returns ``(messages_to_project, cursor)``.

    When the trailing turn is an *executing* in-flight orphan — an
    assistant ``tool_calls`` message whose results aren't all saved yet,
    with the ws NOT awaiting approval — AND the live ring buffer can
    replay the delta past the last resolved-turn boundary, this DROPS the
    orphan turn from ``/history`` and returns ``cursor`` = the resolved
    boundary's ``_event_id``.  The client opens its initial SSE with that
    cursor and the existing ``replay_ok`` path fast-forwards the orphan
    turn whole (content tokens, ``tool_info``, ``tool_result``, …), so the
    committed snapshot and the live delta are disjoint — no double-render,
    no lost siblings (the cursor sits *below* all the orphan's events, so
    out-of-order result saves can't move it).

    Otherwise returns ``(messages, None)`` unchanged, so the connect takes
    the synthetic-snapshot floor:
      - awaiting approval → the ``_pending_approval`` re-emit paints it;
      - reloaded / evicted (empty or truncated buffer) → the orphan keeps
        its #610 history-rendered block (never left unrenderable);
      - quiescent / cursorless history → plain fresh connect.

    Pure + defensive — reads only ``role`` / ``tool_calls`` /
    ``tool_call_id`` / ``_event_id``.  The ``_event_id`` side-channel
    survives reconstruct → decorate → extract_reasoning; this runs on the
    pre-projection list, and ``project_history_messages`` then surfaces it
    as the top-level ``event_id`` for the frontend.
    """
    if awaiting_approval or not messages:
        return messages, None
    can_replay = getattr(ui, "can_replay_from", None)
    if not callable(can_replay):
        return messages, None
    resulted: set[str] = {
        str(m.get("tool_call_id"))
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    # Locate the trailing assistant tool-call turn (break at the first
    # one from the end — mirrors project_history_messages' #610 gate) and
    # whether it still has an unresolved tool_call (an in-flight orphan).
    orphan_idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        tcs = messages[i].get("tool_calls")
        if tcs:
            has_orphan = any(
                (tc.get("id") or "") and str(tc.get("id")) not in resulted for tc in tcs
            )
            orphan_idx = i if has_orphan else None
            break
    if not orphan_idx:  # None (no orphan) or 0 (no resolved boundary before it)
        return messages, None
    resolved_ids = [
        m["_event_id"] for m in messages[:orphan_idx] if isinstance(m.get("_event_id"), int)
    ]
    if not resolved_ids:
        return messages, None  # no committed cursor → snapshot floor
    cursor = max(resolved_ids)
    if not can_replay(cursor):
        return messages, None  # buffer can't fast-forward → #610 floor
    return messages[:orphan_idx], cursor


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

        # Cross-tenant gate.  Pre-PR-447 the response carried only
        # message rows that an owning user wrote and that owning
        # user's tools produced — sensitive but bounded to the same
        # ``user_id`` as the workstream.  Even so, every other lifted
        # session verb (send / approve / close / cancel / events /
        # attachments) calls ``cfg.tenant_check`` and history was the
        # outlier.  Coord wires ``tenant_check=None`` (the
        # cluster-wide ``admin.coordinator`` permission_gate covers
        # it); interactive wires ``_interactive_tenant_check`` and
        # this call now restores parity with the rest of the surface.
        if cfg.tenant_check is not None:
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

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
        live_session = mgr.get(ws_id)
        if live_session is None:
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
        # Fresh-connect resume cursor (the ``Last-Event-ID`` the client
        # opens its initial SSE with).  Non-None only when the trailing
        # turn is an executing in-flight orphan that the ring buffer can
        # fast-forward — see :func:`_resume_cursor_and_trim`.  Stays None
        # on every other path (and on any decoration failure below) so
        # the client takes the synthetic-snapshot floor.
        cursor: int | None = None
        if storage is not None:
            try:
                # repair=False — display read; see reconstruct_messages docstring.
                messages = await asyncio.to_thread(
                    storage.load_messages, ws_id, limit=limit, repair=False
                )
            except Exception:
                log.debug("ws.history.load_failed ws=%s", ws_id[:8], exc_info=True)
        # Audit-trail decoration — attach persisted intent_verdict and
        # output_assessment data to each assistant.tool_calls entry so
        # the dashboard's history replay paints the same verdict pills
        # / output-warning bubbles the live SSE path shows.  Both
        # storage queries are off-loop via ``to_thread``.  Best-effort:
        # any failure leaves messages undecorated — replay degrades to
        # the pre-decoration shape rather than 500-ing.
        if messages:
            try:
                from turnstone.core.history_decoration import (
                    decorate_history_messages,
                    extract_reasoning_for_history,
                    load_verdict_indexes,
                    project_history_messages,
                )

                indexes = await asyncio.to_thread(load_verdict_indexes, ws_id)
                # Pure transform but iterates every message and every
                # tool_call dict — for a long workstream the pass takes
                # tens of milliseconds and would otherwise block the
                # event loop's hot path on the request handler.
                # ``decorate_history_messages`` is thread-safe (no
                # shared mutable state beyond the per-call message
                # list) so the off-loop hop is free.
                await asyncio.to_thread(decorate_history_messages, messages, indexes[0], indexes[1])
                # Active-model ``surface_persisted_reasoning`` flag.  Three-tier
                # resolution so the operator's flag-flip takes effect
                # uniformly — live session, storage-rehydratable cold
                # workstream, or unknown workstream:
                #
                # 1. Live session in memory → read from its registry
                #    (already-warm path).
                # 2. Cold workstream → ``workstream_config.model_alias``
                #    persisted at first send (see
                #    ``session_manager.py:628`` rehydrate path) →
                #    resolve through the kind-appropriate registry on
                #    ``app.state``.
                # 3. Neither available → conservative default ``True``,
                #    matching the migration server_default and the
                #    rehydration default in spec.
                surface_persisted_reasoning = True
                resolved_alias = ""
                resolved_registry: Any = None
                if live_session is not None:
                    resolved_registry = getattr(live_session, "_registry", None)
                    resolved_alias = getattr(live_session, "_model_alias", "") or ""
                if not resolved_alias and storage is not None:
                    # Off-loop the sync storage call (mirrors get_workstream
                    # / load_messages / load_verdict_indexes / decorate /
                    # extract_reasoning_for_history above).  Preserves the
                    # try/except so a DB failure degrades to the
                    # conservative-default branch instead of bubbling out.
                    try:
                        ws_cfg = (
                            await asyncio.to_thread(storage.load_workstream_config, ws_id) or {}
                        )
                    except Exception:
                        ws_cfg = {}
                    resolved_alias = ws_cfg.get("model_alias") or ""
                if resolved_registry is None:
                    # Interactive server stores the registry as
                    # ``app.state.registry``; console stores its coord
                    # registry as ``app.state.coord_registry``.  The
                    # lifted handler is shared, so we try both.
                    resolved_registry = getattr(request.app.state, "registry", None) or getattr(
                        request.app.state, "coord_registry", None
                    )
                if resolved_registry is not None and resolved_alias:
                    try:
                        surface_persisted_reasoning = bool(
                            resolved_registry.get_config(resolved_alias).surface_persisted_reasoning
                        )
                    except Exception:
                        surface_persisted_reasoning = True
                await asyncio.to_thread(
                    extract_reasoning_for_history, messages, surface_persisted_reasoning
                )
                # ``pending`` on the trailing tool-call turn must track the
                # LIVE awaiting-approval signal, not orphan-detection — an
                # executing or interrupted orphan is not awaiting approval
                # and must render its tool block on a fresh connect (else it
                # vanishes until a reconnect replays the buffered events).
                # Read ``_pending_approval`` off the loaded session; a
                # storage-only / closed ws has no live session → never
                # pending.  Stays in lockstep with the approve_request
                # re-emit in the SSE replay (``_interactive_events_replay``).
                # Asserted as ``dict`` (its only real production shape) to
                # match the detail handler below, so a MagicMock-based unit
                # test's auto-vivified attribute doesn't trip the path.
                awaiting_approval = isinstance(
                    getattr(getattr(live_session, "ui", None), "_pending_approval", None),
                    dict,
                )
                # Fresh-connect fast-forward: when the trailing turn is an
                # executing in-flight orphan the ring buffer can replay,
                # drop it from the committed snapshot and hand back a
                # resume cursor so the client's initial SSE rebuilds it via
                # the existing delta replay (disjoint from /history — no
                # double-render).  No-op on every other path (returns the
                # list unchanged + cursor=None).  Runs on the pre-project
                # list while the ``_event_id`` side-channel is still present.
                to_project, cursor = _resume_cursor_and_trim(
                    messages, getattr(live_session, "ui", None), awaiting_approval
                )
                # Final structural projection: flatten nested tool_calls,
                # collapse multipart content, surface the
                # ``_source`` / ``_attachments_meta`` side-channels
                # top-level, and derive
                # ``denied`` / ``is_error`` / ``pending``.  Runs last (reads
                # decorate's in-place verdict/advisory mutations + the
                # stamped ``reasoning``) and returns a fresh list, so the
                # wire payload is the canonical render shape both the
                # interactive ``replayHistory`` and the coordinator history
                # rebuild consume directly — no client-side normaliser.
                messages = await asyncio.to_thread(
                    project_history_messages, to_project, awaiting_approval
                )
                # Task-agent recall: attach each task_agent tool_call's stashed
                # sub-trajectory (projected step items) so the client's
                # ``replayHistory`` can rebuild the collapsible card.  Live
                # in-memory session only — a cold/closed ws, or an entry evicted
                # past the LRU cap, has none, so the card renders the flat parent
                # record ("not retained"), never a fabricated 0-step card.
                # [[HYPOTHESIS]] an unobserved sub-trajectory is unknown, not none.
                get_traj = getattr(getattr(live_session, "ui", None), "get_agent_trajectory", None)
                if get_traj is not None:
                    for msg in messages:
                        for tc in msg.get("tool_calls") or ():
                            # Only task_agent calls ever stash — skip the rest so
                            # we don't take the agent-state lock once per tool_call
                            # on a long history for ids that can never match.
                            if tc.get("name") != "task_agent":
                                continue
                            steps = get_traj(tc.get("id") or "")
                            # Attach only a well-formed, non-empty list — the
                            # ``get_agent_trajectory`` contract is ``list | None``,
                            # and the guard keeps a malformed result out of the
                            # JSON payload (a non-list can't be serialized).
                            if isinstance(steps, list) and steps:
                                tc["agent_steps"] = steps
            except Exception:
                # Operationally interesting: a persistent decoration
                # failure (missing migration, driver mismatch, schema
                # drift) silently strips verdict pills + output
                # warnings from every reload of every workstream.
                # Log at warning so it surfaces in normal log review
                # rather than only when DEBUG is on.  Reset the cursor so a
                # mid-pipeline failure can't pair an un-trimmed orphan with
                # a fast-forward cursor (which would double-render it).
                cursor = None
                log.warning(
                    "ws.history.decoration_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )
        return JSONResponse({"ws_id": ws_id, "messages": messages, "cursor": cursor})

    return history


def make_export_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``GET {prefix}/{ws_id}/export`` — conversation download.

    Serves the workstream's full conversation as an OpenAI-style
    envelope (``{"messages": [...]}``) for download. Reuses the same
    :class:`SessionEndpointConfig` (and therefore the same gate ladder)
    as :func:`make_history_handler`, so ownership + cross-kind isolation
    come for free.

    The HTTP surface is **conversation-only**: it never bundles
    children and always returns ``application/json``. The
    children/zip capability of :func:`export_workstream` is reserved
    for the admin CLI, so the handler calls it with the default
    ``children=False``.

    Per-kind divergence captured by the same fields history consults:

    - ``cfg.permission_gate`` — coord's ``admin.coordinator`` check;
      interactive ``None``.
    - ``cfg.manager_lookup`` — the kind's manager.
    - ``cfg.list_kind`` — required for the storage-fallback kind check
      so an interactive ws_id can't be exported through the coord
      process and vice versa. **Required when this handler is
      mounted** — a missing value fails loud (500 + ``log.error``)
      rather than silently leaking cross-kind history.
    - ``cfg.tenant_check`` — per-``ws_id`` access gate.
    - ``cfg.not_found_label`` — per-kind 404 wording.

    Args:
        cfg: per-kind policy bundle.
    """

    async def export(request: Request) -> Response:
        import asyncio

        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err

        # Fail-closed misconfig gate. Without ``cfg.list_kind`` the
        # storage-fallback path below has no way to enforce cross-kind
        # isolation — an interactive ws_id requested through a coord
        # process would silently export coord history from storage (and
        # vice versa). Mirrors :func:`make_history_handler`'s same gate.
        if cfg.list_kind is None:
            log.error("ws.export.misconfigured_no_list_kind")
            return JSONResponse(
                {"error": "export handler misconfigured"},
                status_code=500,
            )

        mgr_opt, err503 = cfg.manager_lookup(request)
        if err503 is not None:
            return err503
        mgr = cast("SessionManager", mgr_opt)

        ws_id = request.path_params.get("ws_id", "")
        if not ws_id:
            return JSONResponse({"error": "ws_id is required"}, status_code=400)

        # Cross-tenant gate — same posture as history (interactive wires
        # ``_interactive_tenant_check``; coord wires ``None`` and relies
        # on the ``admin.coordinator`` permission_gate above). Always
        # offloaded via ``to_thread`` since the interactive resolver
        # falls through to a synchronous storage read on a cache miss.
        if cfg.tenant_check is not None:
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        # Existence + kind check. The workstream may live only in
        # storage (closed coordinators / persisted-but-not-loaded
        # interactives are still exportable without rehydrating).
        # Mirrors history's ladder: in-memory mgr.get → storage row +
        # kind check → 404. Falling back to storage without the kind
        # check would leak interactive rows through the coord endpoint
        # (and vice versa) on a process that shares storage with the
        # other kind. ``cfg.list_kind`` is guaranteed non-None above.
        storage = getattr(request.app.state, "auth_storage", None)
        live_session = mgr.get(ws_id)
        if live_session is None:
            if storage is None:
                return JSONResponse({"error": cfg.not_found_label}, status_code=404)
            try:
                row = await asyncio.to_thread(storage.get_workstream, ws_id)
            except Exception:
                log.debug("ws.export.lookup_failed ws=%s", ws_id[:8], exc_info=True)
                return JSONResponse({"error": cfg.not_found_label}, status_code=404)
            if row is None or row.get("kind") != cfg.list_kind:
                return JSONResponse({"error": cfg.not_found_label}, status_code=404)

        # Past the gate but no storage handle — the live-session branch
        # above skips the storage requirement, but the serializer needs
        # a real handle. Degrade to the same 404 the fallback uses for a
        # missing storage rather than serving an empty / 500 export.
        if storage is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)

        from starlette.responses import Response as _Response

        from turnstone.core.export import WorkstreamNotFoundError, export_workstream

        # Conversation-only: never bundle children, always JSON.  A live
        # session whose storage row was deleted skips the fallback
        # existence gate above, so guard the serializer's own not-found
        # raise and degrade to the same 404 rather than surfacing a 500.
        try:
            result = await asyncio.to_thread(export_workstream, storage, ws_id)
        except WorkstreamNotFoundError:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        # ws_ids are hex so the filename is already safe, but mirror the
        # attachment download handler's defensive strip of quotes/CR/LF
        # so a future non-hex id can't break the Content-Disposition.
        safe_name = result.filename.replace('"', "").replace("\r", "").replace("\n", "")
        return _Response(
            result.data,
            media_type=result.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )

    return export


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
        import asyncio

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

        # Cross-tenant gate.  PR 447 added the inline approval payload
        # (now ``pending_approval_details``) to the response (tool
        # previews, function arguments, LLM judge reasoning) — a
        # richer payload than the pre-PR
        # ``{ws_id, name, state, user_id, kind}`` tuple.  Coord wires
        # ``tenant_check=None`` (the cluster-wide ``admin.coordinator``
        # permission_gate covers it); interactive wires
        # ``_interactive_tenant_check`` so any authenticated user that
        # GETs another user's ``ws_id`` 404s here instead of reading
        # the in-flight tool-call payload.  Brings detail in line with
        # every other lifted session verb.
        if cfg.tenant_check is not None:
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws = mgr.get(ws_id)
        if ws is None:
            try:
                ws = mgr.open(ws_id)
            except ValueError as exc:
                # Session factory misconfig (e.g. a model alias that no
                # longer resolves). Surface remediation text as 503
                # mirroring :func:`make_open_handler`.
                log.warning("ws.detail.factory_misconfig ws_id=%s exc=%r", ws_id[:8], exc)
                return JSONResponse(
                    {"error": _safe_factory_misconfig_message(exc)}, status_code=503
                )
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

        # Pending-approval snapshot — lets a freshly-loaded chat tab
        # paint the inline approval gate from this single response
        # instead of waiting for the SSE approve_request replay (which
        # introduces a brief --running flash on reload).  Both keys
        # (``pending_approval`` + ``pending_approval_details``) are
        # always present in the response: a UI that doesn't expose
        # ``serialize_pending_approval_details`` (CLI / channel
        # adapters) reports ``False`` / ``[]`` for them.  The
        # ``_pending_approval`` lookup is asserted as ``dict`` (its
        # only real production shape — the oldest-cycle view kept by
        # ``SessionUIBase``) so a MagicMock-based unit test or other
        # non-dict sentinel doesn't trip the path.
        pending_approval = False
        pending_approval_details: list[dict[str, Any]] = []
        ui = ws.ui
        pending_raw = getattr(ui, "_pending_approval", None) if ui is not None else None
        if isinstance(pending_raw, dict):
            pending_approval = True
            # Full per-cycle list — parallel task agents can have
            # several prompts live; the reload path paints them all.
            serializer = getattr(ui, "serialize_pending_approval_details", None)
            if callable(serializer):
                try:
                    maybe_list = serializer()
                    if isinstance(maybe_list, list):
                        pending_approval_details = maybe_list
                except Exception:
                    # Defensive: a malformed verdict object inside the
                    # serializer shouldn't fail the entire detail
                    # response.  The boolean still informs the UI that
                    # an approval is pending; SSE replay carries the
                    # full payload.
                    log.warning(
                        "ws.detail.pending_serialize_failed ws_id=%s",
                        ws_id[:8] if ws_id else "",
                        exc_info=True,
                    )

        return JSONResponse(
            {
                "ws_id": ws.id,
                "name": ws.name,
                "state": ws.state.value,
                "user_id": ws.user_id,
                "kind": ws.kind,
                "pending_approval": pending_approval,
                "pending_approval_details": pending_approval_details,
            }
        )

    return detail


def make_send_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``POST {prefix}/{ws_id}/send`` — message dispatch.

    Resolves any attachment ids the request carries from the per-node upload
    buffer (a peek — the bytes stay buffered for a retry if the queue rejects
    the turn), captures a ``send_id`` tracking token, then dispatches via
    :func:`turnstone.core.session_worker.send` (atomic spawn-or-enqueue under
    ``ws._lock``). The committing ``send`` drains the buffer and writes the
    bytes content-addressed; no reservation is taken or released.

    Capability flags on ``cfg`` toggle the kind-specific behaviour:

    - ``supports_attachments``: when ``False``, the entire
      attachment-resolution block (buffer peek + scope-check)
      short-circuits and any ``attachment_ids`` in the body are
      silently ignored — no resolution, no error. Both kinds wire
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
    from turnstone.core.session import (
        AttachmentsNotQueueableError,
        CrossUserInterjectionError,
        GenerationCancelled,
    )
    from turnstone.core.web_helpers import auth_user_id, read_json_or_400

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
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
            if err_tenant is not None:
                return err_tenant

        ws = mgr.get(ws_id)
        if ws is None:
            return JSONResponse({"error": cfg.not_found_label}, status_code=404)
        ui = ws.ui
        if ui is None:
            return JSONResponse({"error": "session UI not available"}, status_code=409)

        # The authenticated sender: threaded into the fresh-turn dispatch
        # below so per-user MCP credentials follow whoever is actually
        # driving a shared workstream. Deliberately NOT applied on the
        # live-worker queue path — an interjection folds into the current
        # turn under the initiator's identity (no mid-turn credential
        # switch); the next fresh turn rebinds.
        acting_uid = auth_user_id(request)

        # ----- Attachment resolution (from the per-node upload buffer) -----
        send_id = ""
        requested_ids: list[str] = []
        ordered_taken: list[str] = []
        taken_set: set[str] = set()
        resolved_atts: list[Any] = []
        attach_user_id = ""

        if cfg.supports_attachments:
            from turnstone.core.attachment_buffer import get_attachment_buffer
            from turnstone.core.attachments import resolve_staged_attachments

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
            if raw_ids is None:
                # Auto-consume: every pending (staged) upload for this caller,
                # in stage order.
                buffer = get_attachment_buffer()
                requested_ids = [
                    s.attachment_id for s in buffer.list_for(ws_id=ws_id, user_id=attach_user_id)
                ]
            elif isinstance(raw_ids, list) and raw_ids:
                requested_ids = [str(x) for x in raw_ids if x]

            # Peek (not drain): the bytes stay buffered so an attachment-bearing
            # turn the queue rejects can still be retried; the committing
            # ``send`` drains them at write time.  ``resolved`` carries the
            # bytes the session persists content-addressed.
            resolved_atts, ordered_taken, _dropped_resolve = resolve_staged_attachments(
                requested_ids, ws_id, attach_user_id
            )
            taken_set = set(ordered_taken)

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
            return JSONResponse({"error": "No session"}, status_code=500)

        session = ws.session
        # Captured by ``_enqueue`` only when the dispatcher takes the
        # live-worker reuse path. Empty after a fresh-spawn dispatch.
        queue_outcome: dict[str, Any] = {}

        def _enqueue() -> None:
            try:
                cleaned, priority, msg_id = session.queue_message(
                    message,
                    attachment_ids=list(ordered_taken),
                    queue_msg_id=send_id or None,
                    interjector_user_id=acting_uid,
                )
            except AttachmentsNotQueueableError:
                queue_outcome["rejected"] = "attachments_busy"
                return
            except CrossUserInterjectionError:
                # A different authenticated participant tried to interject into
                # someone else's in-flight turn; folding it in would borrow the
                # initiator's credentials and misattribute the message. Reject
                # so they resend as a fresh turn once the worker idles.
                queue_outcome["rejected"] = "cross_user_interjection"
                return
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
                # Fresh turn: rebind per-user MCP credentials to the
                # authenticated sender. Bound here (not via a send()
                # kwarg) so per-kind session stubs with explicit send
                # signatures keep working; getattr-guarded for the same
                # reason. The queue path above never rebinds.
                bind = getattr(session, "bind_acting_user", None)
                if acting_uid and callable(bind):
                    bind(acting_uid)
                session.send(message, **kwargs)
            except GenerationCancelled:
                # Safety net — send() normally handles this internally.
                # If this thread was force-abandoned, ws.worker_thread
                # was set to None — don't emit spurious events.
                if ws.worker_thread is me:
                    _emit_ui("on_stream_end")
                    _emit_ui("on_state_change", "idle")
            except Exception:
                # Undrained staged uploads aren't locked (the buffer is a peek,
                # not a reservation) — they expire on the buffer TTL — so the
                # only cleanup owed here is the UI streaming hook.
                if ws.worker_thread is me:
                    # ``session.send()`` already fired ``on_error``
                    # (with sanitized text), persisted ``last_error``,
                    # and emitted ``state='error'`` via
                    # :meth:`ChatSession._record_fatal_error` before
                    # re-raising.  The route handler only needs the
                    # streaming-cleanup hook the worker contract owes
                    # the UI listeners.
                    _emit_ui("on_stream_end")

        ok = session_worker.send(
            ws,
            enqueue=_enqueue,
            run=_run,
            thread_name=f"send-worker-{ws.id[:8]}",
        )
        if not ok:
            # queue.Full or session-disappeared race — surface as
            # queue_full so clients retry rather than 500. ``attached_ids``
            # is always empty on this path (the dispatch never took
            # ownership); the empty arrays preserve the response-shape
            # guarantee so SDK consumers don't branch on status.
            return JSONResponse(
                {
                    "status": "queue_full",
                    "attached_ids": [],
                    "dropped_attachment_ids": list(requested_ids),
                }
            )

        if queue_outcome.get("rejected") == "attachments_busy":
            # Attachments can't ride a queued user turn (see
            # AttachmentsNotQueueableError for the role-ordering reason).
            # The staged uploads stay in the buffer (peek, not drain) so the
            # client can hold the file and retry once the worker idles.
            return JSONResponse(
                {
                    "status": "attachments_busy",
                    "attached_ids": [],
                    "dropped_attachment_ids": list(requested_ids),
                }
            )

        if queue_outcome.get("rejected") == "cross_user_interjection":
            # A different participant tried to interject into someone else's
            # in-flight turn (see CrossUserInterjectionError). 409 Conflict so
            # the client can surface "wait for the current turn" and resend as
            # a fresh turn under their own identity.
            return JSONResponse(
                {
                    "status": "cross_user_interjection",
                    "error": (
                        "Another participant's turn is in progress. Wait for it "
                        "to finish, then send your message."
                    ),
                    "attached_ids": [],
                    "dropped_attachment_ids": list(requested_ids),
                },
                status_code=409,
            )

        dropped = [aid for aid in requested_ids if aid not in taken_set]
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
                    "attached_ids": list(ordered_taken),
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
                "attached_ids": list(ordered_taken),
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
        from turnstone.core.attachment_buffer import get_attachment_buffer
        from turnstone.core.attachments import PDF_SIZE_CAP
        from turnstone.core.web_helpers import read_multipart_file_or_400

        # The file-classification policy (sniff order, per-kind caps, allowlists)
        # lives in one place — core.attachments.classify_upload — handed in via
        # the upload-helper hook so the console surface can wire it without
        # depending on the node-side server module.
        if cfg.attachment_helpers is None:
            return JSONResponse({"error": "attachment_helpers missing"}, status_code=500)
        classify = cfg.attachment_helpers.classify_upload

        err_gate = await _gate(request)
        if err_gate is not None:
            return err_gate

        ws_id = request.path_params.get("ws_id", "")
        if not ws_id:
            return JSONResponse({"error": "ws_id is required"}, status_code=400)

        user_id, err = await _resolve_owner(request, ws_id)
        if err:
            return err

        got = await read_multipart_file_or_400(request, field="file", max_bytes=PDF_SIZE_CAP)
        if isinstance(got, JSONResponse):
            return got
        filename, claimed_mime, data = got
        if not data:
            return JSONResponse({"error": "Empty file"}, status_code=400)

        kind, mime, rejection = classify(filename, claimed_mime, data)
        if rejection is not None:
            return JSONResponse(
                {"error": rejection.message, "code": rejection.code},
                status_code=rejection.status,
            )
        assert kind is not None and mime is not None  # success ⟹ both set

        # Stage in the per-node upload buffer (content-addressed: the id is the
        # content hash, so re-uploading identical bytes is idempotent).  The
        # bytes are written to storage only at send-commit.  No DB write, no
        # per-user cap — the buffer's size/TTL ceilings bound a flood.
        staged = get_attachment_buffer().stage(
            ws_id=ws_id,
            user_id=user_id,
            filename=filename,
            mime_type=mime,
            kind=kind,
            content=data,
        )
        return JSONResponse(
            {
                "attachment_id": staged.attachment_id,
                "filename": staged.filename,
                "mime_type": staged.mime_type,
                "size_bytes": staged.size_bytes,
                "kind": staged.kind,
            }
        )

    async def list_pending(request: Request) -> Response:
        from turnstone.core.attachment_buffer import get_attachment_buffer

        err_gate = await _gate(request)
        if err_gate is not None:
            return err_gate
        ws_id = request.path_params.get("ws_id", "")
        if not ws_id:
            return JSONResponse({"error": "ws_id is required"}, status_code=400)
        user_id, err = await _resolve_owner(request, ws_id)
        if err:
            return err
        # Pending uploads live in the buffer; project to the same wire shape
        # the DB-backed listing used (no content bytes).
        rows = [
            {
                "attachment_id": s.attachment_id,
                "filename": s.filename,
                "mime_type": s.mime_type,
                "size_bytes": s.size_bytes,
                "kind": s.kind,
            }
            for s in get_attachment_buffer().list_for(ws_id=ws_id, user_id=user_id)
        ]
        return JSONResponse({"attachments": rows})

    async def _resolve_served_blob(
        request: Request,
    ) -> tuple[bytes, str, str, str] | Response:
        """Gate + resolve an attachment blob for serving (content or thumbnail).

        Returns ``(body, kind, mime, filename)`` or an error ``Response``.
        Pending (staged) blobs serve from the buffer scoped to the uploader;
        committed blobs serve from the store gated by ownership — the requester
        (already gated to own ``ws_id``) must have a turn whose ref-list names the
        id.  Cross-user / cross-ws / unreferenced → 404 so existence doesn't leak.
        """
        import asyncio

        from turnstone.core.attachment_buffer import get_attachment_buffer
        from turnstone.core.memory import attachment_referenced_in_ws, get_attachment

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
        staged = get_attachment_buffer().get(attachment_id, ws_id=ws_id, user_id=user_id)
        if staged is not None:
            return (
                staged.content,
                staged.kind,
                staged.mime_type or "application/octet-stream",
                staged.filename or "attachment",
            )
        # Committed-blob gates are sync DB I/O — the ref check is an unbounded
        # ws-scoped LIKE scan (O(turns-in-ws)), so keep it off the event loop.
        row = await asyncio.to_thread(get_attachment, attachment_id)
        if not row or not await asyncio.to_thread(
            attachment_referenced_in_ws, attachment_id, ws_id
        ):
            return JSONResponse({"error": "Not found"}, status_code=404)
        return (
            row.get("content") or b"",
            row.get("kind") or "",
            row.get("mime_type") or "application/octet-stream",
            str(row.get("filename") or "attachment"),
        )

    async def get_content(request: Request) -> Response:
        from starlette.responses import Response as _Response

        resolved = await _resolve_served_blob(request)
        if not isinstance(resolved, tuple):
            return resolved
        body, kind, stored_mime, filename = resolved
        # Force text/plain for text kinds — avoids same-origin HTML/SVG
        # rendering if a user uploaded an HTML-ish text file.  Images keep their
        # sniffed MIME (allowlist is strict: png/jpeg/gif/webp).
        response_mime = "text/plain; charset=utf-8" if kind == "text" else stored_mime
        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Cache-Control": "private, no-store",
        }
        return _Response(body, media_type=response_mime, headers=headers)

    async def get_thumbnail(request: Request) -> Response:
        import asyncio

        from starlette.responses import Response as _Response

        from turnstone.core.thumbnails import make_thumbnail

        resolved = await _resolve_served_blob(request)
        if not isinstance(resolved, tuple):
            return resolved
        body, kind, _mime, _filename = resolved
        if kind not in ("image", "pdf"):
            return JSONResponse({"error": "no thumbnail for this attachment kind"}, status_code=415)
        png = await asyncio.to_thread(make_thumbnail, body, kind)
        if png is None:
            return JSONResponse({"error": "thumbnail unavailable"}, status_code=415)
        return _Response(
            png,
            media_type="image/png",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "private, max-age=300",
            },
        )

    async def delete_(request: Request) -> Response:
        from turnstone.core.attachment_buffer import get_attachment_buffer

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
        # Only pending (staged) uploads are deletable — a committed blob is
        # owned by the turn that references it and is GC'd by refcount.
        deleted = get_attachment_buffer().discard(attachment_id, ws_id=ws_id, user_id=user_id)
        if not deleted:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse({"status": "deleted"})

    return AttachmentHandlers(
        upload=upload,
        list=list_pending,
        get_content=get_content,
        thumbnail=get_thumbnail,
        delete=delete_,
    )


def make_dequeue_handler(cfg: SessionEndpointConfig) -> Handler:
    """Lifted body for ``DELETE {prefix}/{ws_id}/send`` — cancel a queued message.

    Removes a previously-queued message identified by ``msg_id`` from
    the workstream's pending queue. Returns ``status: removed`` when
    the queue had the entry and ``status: not_found`` otherwise.
    Queued messages don't carry attachments (see
    :class:`AttachmentsNotQueueableError`), so there's no reservation
    side-effect to undo here.
    """
    from turnstone.core.web_helpers import read_json_or_400

    async def dequeue(request: Request) -> Response:
        import asyncio

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

        ws_id = request.path_params.get("ws_id", "")
        if cfg.tenant_check is not None:
            err_tenant = await asyncio.to_thread(cfg.tenant_check, request, ws_id, mgr)
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
