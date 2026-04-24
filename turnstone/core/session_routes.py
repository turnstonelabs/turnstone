"""Shared HTTP route registrar for workstream-shaped sessions.

Both node and console processes mount the workstream HTTP tree at
``/v1/api/workstreams/`` via this registrar against their own
:class:`~turnstone.core.session_manager.SessionManager` (interactive
on the node, coordinator on the console). One URL shape, two
processes, kind-specific policy in :class:`SessionEndpointConfig`
that handlers consult at request time via ``app.state``.

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
returned closures (e.g. :func:`make_approve_handler`) that bake the
:class:`SessionEndpointConfig` in at app-construction time. Both
node and console call the factory during startup and pass the
result as ``handlers.approve``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

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


@dataclass(frozen=True)
class SessionEndpointConfig:
    """Per-kind policy the lifted handler bodies consult at request time.

    Instantiated once per process during app construction and stored
    on ``app.state.session_endpoint_config``. The unified handler
    bodies pull this config + the kind manager from ``app.state``
    rather than taking either as a per-request parameter — keeps the
    handler signatures uniform (``Handler = Request -> Response``)
    so the registrar mounts them like any other route.

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
    - ``tenant_check``: per-``ws_id`` cross-tenant guard. Interactive
      uses ``_require_ws_access`` (404 on owner mismatch); coord
      relies on the cluster-wide ``admin.coordinator`` scope from
      ``permission_gate`` and sets this to ``None``.
    - ``not_found_label``: the message body for the 404 returned when
      the manager has no such ws_id ("Workstream not found" for
      interactive; "coordinator not found" for coord).
    - ``audit_action_prefix``: the dot-namespaced prefix the kind
      uses for its audit actions ("workstream" → ``workstream.cancel``;
      "coordinator" → ``coordinator.cancel``).
    """

    permission_gate: PermissionGate | None
    manager_lookup: ManagerLookup
    tenant_check: TenantCheck | None
    not_found_label: str
    audit_action_prefix: str


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
# ``turnstone/console/server.py`` for coord). The lifted body uses
# the kind-specific :class:`SessionEndpointConfig` from
# ``app.state.session_endpoint_config`` to branch on the few places
# the kinds legitimately differ.
#
# Verbs not lifted yet (intentional — bodies have substantive
# behavior divergence that needs SessionManager-side refactoring,
# not just kind branching): send (worker dispatch — Priority 1
# territory), cancel (interactive does inline forensics + force-cancel
# ws._lock manipulation), close (interactive caps + redacts +
# persists close_reason), open (interactive resume vs coord rehydrate),
# events (different SSE replay shapes), create (interactive
# attachments vs coord initial_message), list / saved (different
# response keys: ``workstreams`` vs ``coordinators``).
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
