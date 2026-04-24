"""Shared HTTP route registrar for workstream-shaped sessions.

Stage 2 Priority 0 of the SessionManager unification — collapses the
parallel ``/v1/api/workstreams/*`` (interactive) and
``/v1/api/coordinator/*`` (coordinator, never shipped stable) handler
trees into one. The legacy coord URL prefix has been removed; both
node and console processes mount the same shape against their own
manager via this registrar.

Step 0.1: scaffolded the registrar; interactive ``server.py`` mounts
through it.

Step 0.2: grew the registrar to cover the coord-side verbs (``send``
/ ``approve`` / ``plan`` / ``cancel`` / ``close`` / ``events`` /
``history`` / ``detail``); console mounted coord through it
alongside the legacy ``/v1/api/coordinator/`` paths.

Step 0.3: coord-only verbs (``/trust``, ``/restrict``,
``/stop_cascade``, ``/close_all_children``, ``/children``,
``/metrics``, ``/tasks``) migrated via :func:`register_coord_verbs`.

Step 0.4: legacy ``/v1/api/coordinator/*`` Route entries deleted from
``console/server.py`` — alongside the URL sweep across the OpenAPI
spec, frontend JS, server-side coord client, and test suite (Steps
0.5–0.7). The handler functions stay named ``coordinator_*`` until
the body-convergence follow-on lifts them into this module with
kind branching behind :class:`SessionRouteConfig` flags.

See ``1.5.0-session-manager-stage-2.md`` for the full sequencing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from turnstone.core.session_manager import SessionKindAdapter, SessionManager


Handler = Callable[["Request"], Awaitable["Response"]]


@dataclass(frozen=True)
class SessionRouteHandlers:
    """Bundle of HTTP handler callables the registrar mounts.

    All handlers are optional; ``None`` skips that route. Lets one
    bundle describe both the interactive shape (no per-``{ws_id}``
    ``send`` / ``approve`` etc. yet — those use legacy body-keyed
    paths) and the coord shape (no attachments, no
    ``refresh-title``).

    Step 0.2 placeholder: handlers live in their server modules and
    are passed in here. The next Step 0.2 follow-on lifts the
    bodies into ``session_routes`` and switches kind branching to
    live behind :class:`SessionRouteConfig` flags, at which point
    this struct deletes.
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

    # Legacy interactive close (``ws_id`` in body, not path)
    close_legacy: Handler | None = None  # POST {prefix}/close

    # Per-``{ws_id}`` interaction (coord shape today; interactive
    # adopts these in Priority 1's worker dispatch unification)
    send: Handler | None = None  # POST {prefix}/{ws_id}/send
    approve: Handler | None = None  # POST {prefix}/{ws_id}/approve
    plan: Handler | None = None  # POST {prefix}/{ws_id}/plan
    cancel: Handler | None = None  # POST {prefix}/{ws_id}/cancel
    events: Handler | None = None  # GET  {prefix}/{ws_id}/events (SSE)
    history: Handler | None = None  # GET  {prefix}/{ws_id}/history

    # Attachments (interactive only today; coord parity is post-1.5)
    upload_attachment: Handler | None = None
    list_attachments: Handler | None = None
    get_attachment_content: Handler | None = None
    delete_attachment: Handler | None = None


@dataclass(frozen=True)
class SessionRouteConfig:
    """Per-kind feature flags for the session HTTP route registrar.

    Step 0.2 carries one flag — whether the legacy body-keyed
    ``close`` verb is mounted. Step 0.2's body-convergence follow-on
    grows this with the per-kind branching flags handler bodies
    consult (e.g. ``permission_scope``, ``audit_action_prefix``,
    ``not_found_label``).
    """

    supports_legacy_close: bool = False  # POST {prefix}/close — ws_id in body


def register_session_routes(
    routes: list[BaseRoute],
    *,
    prefix: str,
    mgr: SessionManager | None = None,
    adapter: SessionKindAdapter | None = None,
    config: SessionRouteConfig,
    handlers: SessionRouteHandlers,
) -> None:
    """Append the workstream HTTP route table to ``routes`` at ``prefix``.

    Mounts every verb whose handler is non-``None``. Routes register
    in an order that respects Starlette's first-match semantics:
    literal subpaths (``saved``, ``new``, ``close``) before the
    per-``{ws_id}`` patterns; per-``{ws_id}/{verb}`` patterns before
    the bare ``{ws_id}`` detail GET.

    Args:
        routes: list to extend; typically the inner ``Mount`` route
            list a Starlette app uses.
        prefix: URL prefix relative to the mount, e.g.
            ``"/api/workstreams"``.
        mgr: the session manager owning the workstream kind, when
            available at app-construction time. Optional today
            because the console builds its coord manager in the
            lifespan (after routes), and current handler bodies
            look the manager up via ``request.app.state``. Becomes
            required in the body-convergence follow-on once handlers
            move into this module and read ``mgr`` directly.
        adapter: the kind's adapter. Same forward-wiring rationale.
        config: per-kind feature flags.
        handlers: bundle of handler callables for the routes the
            registrar mounts. The body-convergence follow-on
            deletes this argument.
    """
    # ``mgr`` and ``adapter`` are unused this commit; declared in the
    # signature so handler bodies can pick them up directly in the
    # body-convergence follow-on without a callsite churn.
    _ = (mgr, adapter)

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
    if config.supports_legacy_close:
        if handlers.close_legacy is None:
            raise ValueError("supports_legacy_close=True requires handlers.close_legacy")
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

    # --- Attachments ----------------------------------------------------
    has_any_attachment_handler = (
        handlers.upload_attachment is not None
        or handlers.list_attachments is not None
        or handlers.get_attachment_content is not None
        or handlers.delete_attachment is not None
    )
    if has_any_attachment_handler:
        # Either all four come together or it's a config error — partial
        # attachment surfaces (e.g. upload without delete) leave broken
        # frontend flows.
        if (
            handlers.upload_attachment is None
            or handlers.list_attachments is None
            or handlers.get_attachment_content is None
            or handlers.delete_attachment is None
        ):
            raise ValueError("attachment handlers must be set as a complete set of four")
        routes.append(
            Route(
                f"{p}/{{ws_id}}/attachments",
                handlers.upload_attachment,
                methods=["POST"],
            )
        )
        routes.append(
            Route(
                f"{p}/{{ws_id}}/attachments",
                handlers.list_attachments,
                methods=["GET"],
            )
        )
        routes.append(
            Route(
                f"{p}/{{ws_id}}/attachments/{{attachment_id}}/content",
                handlers.get_attachment_content,
                methods=["GET"],
            )
        )
        routes.append(
            Route(
                f"{p}/{{ws_id}}/attachments/{{attachment_id}}",
                handlers.delete_attachment,
                methods=["DELETE"],
            )
        )

    # --- Bare ``{ws_id}`` detail (GET) registers LAST so the verb-
    #     suffixed patterns above win for ``{ws_id}/...`` paths.
    if handlers.detail is not None:
        routes.append(Route(f"{p}/{{ws_id}}", handlers.detail, methods=["GET"]))


@dataclass(frozen=True)
class CoordVerbHandlers:
    """Bundle of coord-only HTTP handler callables.

    These verbs are legitimately kind-specific (they read or mutate
    coord-only state — children registry, parent quota, trust /
    restrict policy, cascade controls) so they live on a separate
    Protocol from :class:`SessionRouteHandlers`. Mounted alongside
    the shared session verbs at the same ``/api/workstreams/{ws_id}/``
    prefix so the URL surface stays unified, but registered through
    a distinct call so the kind separation is explicit at the wiring
    site.

    Step 0.3 placeholder: handlers live in
    ``turnstone/console/server.py`` and are passed in here; the
    body-convergence follow-on lifts them into a coord-specific
    module.
    """

    children: Handler  # GET  {prefix}/{ws_id}/children
    tasks: Handler  # GET  {prefix}/{ws_id}/tasks
    metrics: Handler  # GET  {prefix}/{ws_id}/metrics
    trust: Handler  # POST {prefix}/{ws_id}/trust
    restrict: Handler  # POST {prefix}/{ws_id}/restrict
    stop_cascade: Handler  # POST {prefix}/{ws_id}/stop_cascade
    close_all_children: Handler  # POST {prefix}/{ws_id}/close_all_children


def register_coord_verbs(
    routes: list[BaseRoute],
    *,
    prefix: str,
    mgr: SessionManager | None = None,
    handlers: CoordVerbHandlers,
) -> None:
    """Mount coord-only verbs at the unified ``{prefix}/{ws_id}/...`` shape.

    Call ordering vs :func:`register_session_routes` doesn't matter
    in practice — Starlette's default ``str`` path converter is
    single-segment, so ``{ws_id}/{verb}`` patterns can never collide
    with the bare ``{ws_id}`` detail GET registered by
    ``register_session_routes``. The body-convergence follow-on will
    additionally have these handlers 404 on non-coord ws_ids via the
    manager's kind check; today the legacy ``admin.coordinator``
    permission gate and ``_require_coord_mgr`` 503 are the
    enforcement.

    Args:
        routes: list to extend; typically the inner ``Mount`` route
            list a Starlette app uses.
        prefix: URL prefix relative to the mount, e.g.
            ``"/api/workstreams"``.
        mgr: the coord session manager when available at
            app-construction time. Optional today because the console
            builds its coord manager in the lifespan; becomes required
            in the body-convergence follow-on.
        handlers: bundle of handler callables for the coord-only
            verbs.
    """
    _ = mgr  # forward-wired; see :func:`register_session_routes`

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
