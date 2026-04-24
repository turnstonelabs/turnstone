"""Tests for the shared session HTTP route registrar.

Stage 2 Priority 0 Step 0.2 — verifies that
:func:`turnstone.core.session_routes.register_session_routes` mounts
the right route table per the configured handler bundle, and that
the console's ``create_app`` exposes the unified
``/v1/api/workstreams/`` URL shape for coord alongside the legacy
``/v1/api/coordinator/`` paths during the transition window.

Body-level behavior is covered by the per-kind endpoint tests
(``tests/test_workstream_endpoints.py``,
``tests/test_coordinator_endpoints.py``); this module checks only the
routing surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.responses import JSONResponse
from starlette.routing import Route

from turnstone.core.session_routes import (
    SessionRouteConfig,
    SessionRouteHandlers,
    register_session_routes,
)

if TYPE_CHECKING:
    from starlette.requests import Request


async def _stub(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


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
        config=SessionRouteConfig(),
        handlers=SessionRouteHandlers(),
    )
    assert routes == []


def test_coord_shape_mounts_expected_verbs() -> None:
    """Coord wiring exposes list / saved / new / detail + the
    per-``{ws_id}`` interaction verbs at the unified prefix."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        config=SessionRouteConfig(),
        handlers=SessionRouteHandlers(
            list_workstreams=_stub,
            list_saved=_stub,
            create=_stub,
            detail=_stub,
            open=_stub,
            close=_stub,
            send=_stub,
            approve=_stub,
            cancel=_stub,
            events=_stub,
            history=_stub,
        ),
    )
    paths = _route_paths(routes)
    expected = {
        ("/api/workstreams", frozenset({"GET", "HEAD"})),
        ("/api/workstreams/saved", frozenset({"GET", "HEAD"})),
        ("/api/workstreams/new", frozenset({"POST"})),
        ("/api/workstreams/{ws_id}/open", frozenset({"POST"})),
        ("/api/workstreams/{ws_id}/close", frozenset({"POST"})),
        ("/api/workstreams/{ws_id}/send", frozenset({"POST"})),
        ("/api/workstreams/{ws_id}/approve", frozenset({"POST"})),
        ("/api/workstreams/{ws_id}/cancel", frozenset({"POST"})),
        ("/api/workstreams/{ws_id}/events", frozenset({"GET", "HEAD"})),
        ("/api/workstreams/{ws_id}/history", frozenset({"GET", "HEAD"})),
        ("/api/workstreams/{ws_id}", frozenset({"GET", "HEAD"})),
    }
    assert set(paths) == expected


def test_interactive_shape_mounts_legacy_close_and_attachments() -> None:
    """Interactive wiring exposes legacy body-keyed close + the
    full attachments quartet under the unified prefix."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        config=SessionRouteConfig(supports_legacy_close=True),
        handlers=SessionRouteHandlers(
            list_workstreams=_stub,
            list_saved=_stub,
            create=_stub,
            close_legacy=_stub,
            delete=_stub,
            open=_stub,
            refresh_title=_stub,
            set_title=_stub,
            upload_attachment=_stub,
            list_attachments=_stub,
            get_attachment_content=_stub,
            delete_attachment=_stub,
        ),
    )
    paths = {(p, m) for p, m in _route_paths(routes)}
    assert ("/api/workstreams/close", frozenset({"POST"})) in paths
    assert ("/api/workstreams/{ws_id}/delete", frozenset({"POST"})) in paths
    assert ("/api/workstreams/{ws_id}/refresh-title", frozenset({"POST"})) in paths
    assert ("/api/workstreams/{ws_id}/title", frozenset({"POST"})) in paths
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


def test_supports_legacy_close_without_handler_raises() -> None:
    routes: list[Any] = []
    with pytest.raises(ValueError, match="close_legacy"):
        register_session_routes(
            routes,
            prefix="/api/workstreams",
            config=SessionRouteConfig(supports_legacy_close=True),
            handlers=SessionRouteHandlers(),
        )


def test_partial_attachment_handlers_raise() -> None:
    """Setting one or two attachment handlers without all four is a
    config error — partial surfaces leave broken frontend flows."""
    routes: list[Any] = []
    with pytest.raises(ValueError, match="attachment handlers"):
        register_session_routes(
            routes,
            prefix="/api/workstreams",
            config=SessionRouteConfig(),
            handlers=SessionRouteHandlers(
                upload_attachment=_stub,
                list_attachments=_stub,
                # missing get_attachment_content + delete_attachment
            ),
        )


def test_saved_registers_before_detail() -> None:
    """Literal ``saved`` must register before bare ``{ws_id}`` so
    Starlette doesn't match "saved" as a ws_id path param."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        config=SessionRouteConfig(),
        handlers=SessionRouteHandlers(
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
        config=SessionRouteConfig(),
        handlers=SessionRouteHandlers(
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


def test_console_create_app_exposes_unified_workstream_paths() -> None:
    """The console's ``create_app`` mounts coord verbs at both the
    legacy ``/api/coordinator/`` shape and the unified
    ``/api/workstreams/`` shape so SDK consumers can migrate
    incrementally before the legacy paths delete in Step 0.4.
    """
    from tests.test_console import MockStorage
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import create_app

    collector = ClusterCollector(storage=MockStorage(), discovery_interval=999)
    app = create_app(collector=collector)
    # Walk the route tree to collect every mounted path.
    paths: set[str] = set()

    def _walk(routes: Any) -> None:
        for r in routes:
            if hasattr(r, "path"):
                paths.add(r.path)
            sub = getattr(r, "routes", None)
            if sub:
                _walk(sub)

    _walk(app.routes)
    # Legacy shape still present (Step 0.2 transition; Step 0.4 deletes).
    assert any("/api/coordinator/" in p or p.endswith("/api/coordinator") for p in paths)
    # Unified shape is now also present.
    assert "/v1/api/workstreams" in paths or "/api/workstreams" in paths
    assert any(p.endswith("/api/workstreams/{ws_id}/send") for p in paths)
    assert any(p.endswith("/api/workstreams/{ws_id}/approve") for p in paths)
    assert any(p.endswith("/api/workstreams/{ws_id}/events") for p in paths)
    assert any(p.endswith("/api/workstreams/{ws_id}") for p in paths)


def test_console_unified_paths_route_to_legacy_handlers() -> None:
    """Each unified ``/api/workstreams/{verb}`` route on the console
    points at the SAME handler function as the legacy
    ``/api/coordinator/{verb}`` route — the transition is a pure
    URL alias, not a fork.
    """
    from tests.test_console import MockStorage
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import create_app

    collector = ClusterCollector(storage=MockStorage(), discovery_interval=999)
    app = create_app(collector=collector)

    endpoint_by_path: dict[str, Any] = {}

    def _walk(routes: Any) -> None:
        for r in routes:
            if isinstance(r, Route):
                endpoint_by_path[r.path] = r.endpoint
            sub = getattr(r, "routes", None)
            if sub:
                _walk(sub)

    _walk(app.routes)

    # Spot-check the verb pairs whose handlers must remain identical.
    aliases = {
        "/api/coordinator": "/api/workstreams",
        "/api/coordinator/saved": "/api/workstreams/saved",
        "/api/coordinator/new": "/api/workstreams/new",
        "/api/coordinator/{ws_id}/send": "/api/workstreams/{ws_id}/send",
        "/api/coordinator/{ws_id}/approve": "/api/workstreams/{ws_id}/approve",
        "/api/coordinator/{ws_id}/cancel": "/api/workstreams/{ws_id}/cancel",
        "/api/coordinator/{ws_id}/close": "/api/workstreams/{ws_id}/close",
        "/api/coordinator/{ws_id}/open": "/api/workstreams/{ws_id}/open",
        "/api/coordinator/{ws_id}/events": "/api/workstreams/{ws_id}/events",
        "/api/coordinator/{ws_id}/history": "/api/workstreams/{ws_id}/history",
        "/api/coordinator/{ws_id}": "/api/workstreams/{ws_id}",
    }
    # Walked Route paths are relative to their parent Mount; the
    # ``/v1`` prefix is applied at request time, not stored on the
    # nested Route object.
    for legacy, unified in aliases.items():
        assert legacy in endpoint_by_path, f"missing legacy path {legacy}"
        assert unified in endpoint_by_path, f"missing unified path {unified}"
        assert endpoint_by_path[legacy] is endpoint_by_path[unified], (
            f"{unified} routes to a different handler than {legacy}"
        )
