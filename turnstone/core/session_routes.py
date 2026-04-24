"""Shared HTTP route registrar for workstream-shaped sessions.

Stage 2 Priority 0 of the SessionManager unification — collapses the
parallel ``/v1/api/workstreams/*`` (interactive) and
``/v1/api/coordinator/*`` (coordinator) handler trees into one.

Step 0.1 (this commit): scaffold the registrar. Handler bodies still
live in ``turnstone/server.py`` and ``turnstone/console/server.py``
and are passed in via :class:`SessionRouteHandlers`. The registrar
owns the URL shape; the next step lifts bodies into this module so
both kinds share them.

Step 0.2: coord migrates into the registrar; per-kind branching moves
behind :class:`SessionRouteConfig` flags so each kind's call site is
localized rather than spread across two server files.

Step 0.3: coord-only verbs (``/trust``, ``/restrict``,
``/stop_cascade``, ``/close_all_children``, ``/children``,
``/metrics``, ``/tasks``) migrate via :func:`register_coord_verbs`,
404-ing on non-coord ws_ids via the manager's kind check.

Step 0.4: ``/v1/api/coordinator/*`` deletes outright. The window is
open precisely because that prefix never shipped in a stable release;
once 1.5.0 stable tags it becomes a committed surface.

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

    Step 0.1 placeholder: handlers live in their server modules and
    are passed in here. Step 0.2 lifts the bodies into
    ``session_routes`` and switches kind branching to live behind
    :class:`SessionRouteConfig` flags, at which point this struct
    deletes.

    Attachment + send-family handlers are ``None``-able because
    interactive currently exposes ``/api/send`` etc. without a path
    ``ws_id`` (legacy URL shape) and coord doesn't expose attachments
    at all yet. Step 0.2 normalizes those gaps.
    """

    list_workstreams: Handler
    list_saved: Handler
    create: Handler
    close_legacy: Handler  # POST /workstreams/close — ws_id in body, interactive only
    delete: Handler
    open: Handler
    refresh_title: Handler
    set_title: Handler
    upload_attachment: Handler | None = None
    list_attachments: Handler | None = None
    get_attachment_content: Handler | None = None
    delete_attachment: Handler | None = None


@dataclass(frozen=True)
class SessionRouteConfig:
    """Per-kind feature flags for the session HTTP route registrar.

    Step 0.1 carries only the flags the current registrar branches
    on; Step 0.2 grows the flag set as kind-specific handler bodies
    are lifted in (e.g. ``supports_initial_message_worker`` for
    interactive ``/new``, ``supports_parent_ws_id`` for coord
    ``/new``, kind-aware permission scope for the auth gate).
    """

    supports_attachments: bool = False
    supports_legacy_close: bool = False  # interactive: POST /workstreams/close — ws_id in body


def register_session_routes(
    routes: list[BaseRoute],
    *,
    prefix: str,
    mgr: SessionManager,
    adapter: SessionKindAdapter,
    config: SessionRouteConfig,
    handlers: SessionRouteHandlers,
) -> None:
    """Append the workstream HTTP route table to ``routes`` at ``prefix``.

    Mounts the per-workstream verbs (``new``, ``close``, ``open``,
    ``delete``, ``title``, ``refresh-title``, attachments) plus the
    list/saved listing endpoints under ``prefix``. Routes that are
    not workstream-prefixed in the current interactive surface
    (``/api/dashboard``, ``/api/send``, ``/api/approve``,
    ``/api/plan``, ``/api/cancel``, ``/api/command``, ``/api/events``)
    stay at their existing paths in ``server.py`` for Step 0.1 — the
    next step migrates them into a unified ``{prefix}/{ws_id}/...``
    shape so coord can share the handlers.

    Args:
        routes: list to extend; typically the inner ``Mount`` route
            list a Starlette app uses.
        prefix: URL prefix relative to the mount, e.g.
            ``"/api/workstreams"``.
        mgr: the session manager owning the workstream kind. Wired
            through now so Step 0.2's lifted handler bodies can read
            it without further plumbing.
        adapter: the kind's adapter. Same forward-wiring rationale.
        config: per-kind feature flags.
        handlers: bundle of handler callables for the routes the
            registrar mounts. Step 0.2 deletes this argument.
    """
    # ``mgr`` and ``adapter`` are unused in Step 0.1; declared in the
    # signature so handler bodies can pick them up directly in 0.2
    # without a callsite churn. Suppress the lint locally.
    _ = (mgr, adapter)

    p = prefix.rstrip("/")

    # --- Listing endpoints ----------------------------------------------
    routes.append(Route(p, handlers.list_workstreams))
    # ``saved`` literal must register BEFORE the ``{ws_id}`` routes
    # below so Starlette doesn't match "saved" as a ws_id path param.
    routes.append(Route(f"{p}/saved", handlers.list_saved))

    # --- Lifecycle: create + legacy close (ws_id in body) ---------------
    routes.append(Route(f"{p}/new", handlers.create, methods=["POST"]))
    if config.supports_legacy_close:
        routes.append(Route(f"{p}/close", handlers.close_legacy, methods=["POST"]))

    # --- Per-{ws_id} verbs ----------------------------------------------
    routes.append(Route(f"{p}/{{ws_id}}/delete", handlers.delete, methods=["POST"]))
    routes.append(Route(f"{p}/{{ws_id}}/open", handlers.open, methods=["POST"]))
    routes.append(
        Route(
            f"{p}/{{ws_id}}/refresh-title",
            handlers.refresh_title,
            methods=["POST"],
        )
    )
    routes.append(Route(f"{p}/{{ws_id}}/title", handlers.set_title, methods=["POST"]))

    # --- Attachments (interactive-only today; coord parity is post-1.5) -
    if config.supports_attachments:
        if (
            handlers.upload_attachment is None
            or handlers.list_attachments is None
            or handlers.get_attachment_content is None
            or handlers.delete_attachment is None
        ):
            raise ValueError("supports_attachments=True requires all four attachment handlers")
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
