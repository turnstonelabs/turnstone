"""Tests for the shared session HTTP route registrar.

Verifies that :func:`turnstone.core.session_routes.register_session_routes`
and :func:`turnstone.core.session_routes.register_coord_verbs` mount
the right route table per the supplied handler bundles, and that the
console's ``create_app`` exposes the unified ``/v1/api/workstreams/``
URL shape (the legacy ``/v1/api/coordinator/`` shape is gone).

Body-level behavior is covered by the per-kind endpoint tests
(``tests/test_workstream_endpoints.py``,
``tests/test_coordinator_endpoints.py``); this module checks only the
routing surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse
from starlette.routing import Route

from turnstone.core.session_routes import (
    AttachmentHandlers,
    CoordOnlyVerbHandlers,
    SharedSessionVerbHandlers,
    register_coord_verbs,
    register_session_routes,
)

if TYPE_CHECKING:
    from starlette.requests import Request


async def _stub(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _attach() -> AttachmentHandlers:
    return AttachmentHandlers(upload=_stub, list=_stub, get_content=_stub, delete=_stub)


def _route_paths(routes: list[Any]) -> list[tuple[str, frozenset[str]]]:
    out = []
    for r in routes:
        assert isinstance(r, Route)
        out.append((r.path, frozenset(r.methods or set())))
    return out


def test_empty_handlers_register_no_routes() -> None:
    """A handler bundle with everything ``None`` mounts zero routes."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(),
    )
    assert routes == []


def test_saved_registers_before_detail() -> None:
    """Literal ``saved`` must register before bare ``{ws_id}`` so
    Starlette doesn't match "saved" as a ws_id path param."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(
            list_saved=_stub,
            detail=_stub,
        ),
    )
    paths = [r.path for r in routes if isinstance(r, Route)]
    assert paths.index("/api/workstreams/saved") < paths.index("/api/workstreams/{ws_id}")


def test_specific_verbs_register_before_bare_detail() -> None:
    """Per-verb ``{ws_id}/{verb}`` patterns must register before the
    bare ``{ws_id}`` GET so Starlette routes verb requests to the
    right handler."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(
            detail=_stub,
            close=_stub,
            send=_stub,
            events=_stub,
        ),
    )
    paths = [r.path for r in routes if isinstance(r, Route)]
    detail_idx = paths.index("/api/workstreams/{ws_id}")
    assert paths.index("/api/workstreams/{ws_id}/close") < detail_idx
    assert paths.index("/api/workstreams/{ws_id}/send") < detail_idx
    assert paths.index("/api/workstreams/{ws_id}/events") < detail_idx


def test_attachment_routes_mount_when_quartet_provided() -> None:
    """All four attachment routes mount when ``handlers.attachments``
    is non-``None`` — the type system requires the four-handler
    quartet to be set together."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(attachments=_attach()),
    )
    paths = {(p, m) for p, m in _route_paths(routes)}
    assert ("/api/workstreams/{ws_id}/attachments", frozenset({"POST"})) in paths
    assert ("/api/workstreams/{ws_id}/attachments", frozenset({"GET", "HEAD"})) in paths
    assert (
        "/api/workstreams/{ws_id}/attachments/{attachment_id}/content",
        frozenset({"GET", "HEAD"}),
    ) in paths
    assert (
        "/api/workstreams/{ws_id}/attachments/{attachment_id}",
        frozenset({"DELETE"}),
    ) in paths


def test_send_mounts_post_and_delete_when_dequeue_provided() -> None:
    """``handlers.send`` mounts POST {prefix}/{ws_id}/send and
    ``handlers.dequeue`` mounts DELETE on the same path. The two
    routes register as separate ``Route`` entries with disjoint
    method sets — Starlette dispatches by (path, method)."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(send=_stub, dequeue=_stub),
    )
    paths = {(p, m) for p, m in _route_paths(routes)}
    assert ("/api/workstreams/{ws_id}/send", frozenset({"POST"})) in paths
    assert ("/api/workstreams/{ws_id}/send", frozenset({"DELETE"})) in paths

    # ``dequeue`` is independent of ``send`` — providing it alone
    # mounts only the DELETE half (no POST regression).
    routes_dequeue_only: list[Any] = []
    register_session_routes(
        routes_dequeue_only,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(dequeue=_stub),
    )
    paths_dequeue_only = {(p, m) for p, m in _route_paths(routes_dequeue_only)}
    assert ("/api/workstreams/{ws_id}/send", frozenset({"DELETE"})) in paths_dequeue_only
    assert ("/api/workstreams/{ws_id}/send", frozenset({"POST"})) not in paths_dequeue_only


def test_close_legacy_mounts_when_handler_provided() -> None:
    """The legacy body-keyed close (``POST {prefix}/close``) mounts
    when ``handlers.close_legacy`` is non-``None`` — there is no
    separate config flag, just the handler's presence."""
    routes_with: list[Any] = []
    register_session_routes(
        routes_with,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(close_legacy=_stub),
    )
    assert any(r.path == "/api/workstreams/close" for r in routes_with if isinstance(r, Route))

    routes_without: list[Any] = []
    register_session_routes(
        routes_without,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(),
    )
    assert not any(
        r.path == "/api/workstreams/close" for r in routes_without if isinstance(r, Route)
    )


def test_register_coord_verbs_mounts_seven_paths() -> None:
    """``register_coord_verbs`` mounts the seven coord-only verbs
    at the unified prefix."""
    routes: list[Any] = []
    register_coord_verbs(
        routes,
        prefix="/api/workstreams",
        handlers=CoordOnlyVerbHandlers(
            children=_stub,
            tasks=_stub,
            metrics=_stub,
            trust=_stub,
            restrict=_stub,
            stop_cascade=_stub,
            close_all_children=_stub,
        ),
    )
    paths = {(p, m) for p, m in _route_paths(routes)}
    assert paths == {
        ("/api/workstreams/{ws_id}/children", frozenset({"GET", "HEAD"})),
        ("/api/workstreams/{ws_id}/tasks", frozenset({"GET", "HEAD"})),
        ("/api/workstreams/{ws_id}/metrics", frozenset({"GET", "HEAD"})),
        ("/api/workstreams/{ws_id}/trust", frozenset({"POST"})),
        ("/api/workstreams/{ws_id}/restrict", frozenset({"POST"})),
        ("/api/workstreams/{ws_id}/stop_cascade", frozenset({"POST"})),
        ("/api/workstreams/{ws_id}/close_all_children", frozenset({"POST"})),
    }


def test_console_create_app_only_mounts_unified_workstream_paths() -> None:
    """The console's ``create_app`` mounts coord verbs only at the
    unified ``/api/workstreams/`` shape — no path under
    ``/api/coordinator/`` should remain (deleted in Step 0.4)."""
    from tests._coord_test_helpers import MockStorage
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import create_app

    collector = ClusterCollector(storage=MockStorage(), discovery_interval=999)
    app = create_app(collector=collector)
    paths: set[str] = set()

    def _walk(routes: Any) -> None:
        for r in routes:
            if hasattr(r, "path"):
                paths.add(r.path)
            sub = getattr(r, "routes", None)
            if sub:
                _walk(sub)

    _walk(app.routes)
    assert not any("/api/coordinator" in p for p in paths), (
        f"legacy /api/coordinator paths still mounted: "
        f"{sorted(p for p in paths if '/api/coordinator' in p)}"
    )
    assert any(p.endswith("/api/workstreams") for p in paths)
    # Spot-check one verb per category from the registrar.
    assert any(p.endswith("/api/workstreams/{ws_id}/send") for p in paths)
    assert any(p.endswith("/api/workstreams/{ws_id}/events") for p in paths)
    assert any(p.endswith("/api/workstreams/{ws_id}") for p in paths)
    # And one from the coord-only registrar.
    assert any(p.endswith("/api/workstreams/{ws_id}/trust") for p in paths)
    assert any(p.endswith("/api/workstreams/{ws_id}/close_all_children") for p in paths)
