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

from collections.abc import Awaitable, Callable
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
    from turnstone.core.workstream import Workstream

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
