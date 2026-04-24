"""Shared HTTP route registrar for workstream-shaped sessions.

Both node and console processes mount the workstream HTTP tree at
``/v1/api/workstreams/`` via this registrar against their own
:class:`~turnstone.core.session_manager.SessionManager` (interactive
on the node, coordinator on the console). One URL shape, two
processes, kind-specific handler bodies wired in by the caller.

Two registrar functions:

- :func:`register_session_routes` — verbs both kinds expose
  (``new``, ``close``, ``open``, ``delete``, ``send``, ``approve``,
  ``cancel``, ``events``, ``history``, ``detail``, ...).
  All handlers in :class:`SharedSessionVerbHandlers` are optional;
  ``None`` skips the route, so one bundle describes either kind.
- :func:`register_coord_verbs` — coord-only verbs (``trust``,
  ``restrict``, ``stop_cascade``, ``close_all_children``,
  ``children``, ``tasks``, ``metrics``) that read or mutate state
  that doesn't exist on interactive workstreams.
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


Handler = Callable[["Request"], Awaitable["Response"]]


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
