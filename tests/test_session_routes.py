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
    return AttachmentHandlers(
        upload=_stub, list=_stub, get_content=_stub, thumbnail=_stub, preview=_stub, delete=_stub
    )


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


def test_rewind_retry_register_before_bare_detail() -> None:
    """``/rewind`` and ``/retry`` (issue #549) mount as POST verbs
    before the bare ``{ws_id}`` GET, like the other interaction verbs."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(
            detail=_stub,
            rewind=_stub,
            retry=_stub,
        ),
    )
    paths = [r.path for r in routes if isinstance(r, Route)]
    detail_idx = paths.index("/api/workstreams/{ws_id}")
    assert paths.index("/api/workstreams/{ws_id}/rewind") < detail_idx
    assert paths.index("/api/workstreams/{ws_id}/retry") < detail_idx
    by_path = {p: m for p, m in _route_paths(routes)}
    assert "POST" in by_path["/api/workstreams/{ws_id}/rewind"]
    assert "POST" in by_path["/api/workstreams/{ws_id}/retry"]


def test_command_registers_before_bare_detail() -> None:
    """``/command`` mounts as a POST verb before the bare ``{ws_id}`` GET."""
    routes: list[Any] = []
    register_session_routes(
        routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(detail=_stub, command=_stub),
    )
    paths = [r.path for r in routes if isinstance(r, Route)]
    assert paths.index("/api/workstreams/{ws_id}/command") < paths.index("/api/workstreams/{ws_id}")
    assert "POST" in dict(_route_paths(routes))["/api/workstreams/{ws_id}/command"]


def test_attachment_routes_mount_when_quintet_provided() -> None:
    """All five attachment routes mount when ``handlers.attachments``
    is non-``None`` — the type system requires the five-handler
    set to be provided together."""
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
        "/api/workstreams/{ws_id}/attachments/{attachment_id}/thumbnail",
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
    assert any(p.endswith("/api/workstreams/{ws_id}/command") for p in paths)
    assert any(p.endswith("/api/workstreams/{ws_id}") for p in paths)
    # And one from the coord-only registrar.
    assert any(p.endswith("/api/workstreams/{ws_id}/trust") for p in paths)
    assert any(p.endswith("/api/workstreams/{ws_id}/close_all_children") for p in paths)


# ---------------------------------------------------------------------------
# model_not_loaded_response (#1359)
# ---------------------------------------------------------------------------


def _not_loaded(
    alias: str,
    *,
    reason: str = "",
    retryable: bool = False,
    wording: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """The refusal body for *alias*, built as production builds it: the error
    carries the registry's reason, and the wording is a kind's own constant."""
    import json

    from turnstone.core.model_registry import UnknownModelAliasError
    from turnstone.core.session_routes import NODE_MODEL_WORDING, model_not_loaded_response

    exc = UnknownModelAliasError(alias, retryable=retryable, reason=reason)
    resp = model_not_loaded_response(exc, wording=wording or NODE_MODEL_WORDING, **kwargs)
    assert resp.status_code == 503
    body: dict[str, Any] = json.loads(bytes(resp.body))
    assert body["code"] == "model_not_loaded"
    return body


def test_not_loaded_names_the_default() -> None:
    body = _not_loaded("local-qwen", requested=False)
    assert body["alias"] == "local-qwen"
    assert body["error"] == (
        "The default model 'local-qwen' isn't loaded on this node. "
        "Ask an admin to check its definition."
    )


def test_not_loaded_never_points_at_another_model() -> None:
    # A coordinator reads this through its spawn tool.
    for kwargs in ({"requested": False}, {"requested": True}, {"requested": False, "reopen": True}):
        assert "another model" not in _not_loaded("x", **kwargs)["error"]
    assert "Choose" not in _not_loaded("", requested=False)["error"]


def test_not_loaded_suggests_retrying_a_store_outage() -> None:
    body = _not_loaded(
        "local-qwen",
        requested=False,
        retryable=True,
        reason="the secret store holding its API key can't be reached",
    )
    assert body["error"] == (
        "The default model 'local-qwen' isn't loaded on this node: the secret store "
        "holding its API key can't be reached. Try again shortly."
    )


def test_not_loaded_names_the_registrys_reason() -> None:
    body = _not_loaded(
        "typo", requested=True, reason="no enabled model definition by that name is loaded"
    )
    assert body["error"] == (
        "The model 'typo' isn't loaded on this node: no enabled model definition by that "
        "name is loaded. Ask an admin to check its definition."
    )


def test_a_reopen_suggests_a_retry_only_when_it_may_clear() -> None:
    def _reopen(retryable: bool) -> str:
        body = _not_loaded("gone", requested=False, reopen=True, retryable=retryable)
        return str(body["error"])

    assert _reopen(False) == (
        "Neither this workstream's model nor the default model 'gone' is loaded on "
        "this node. Ask an admin to check them."
    )
    assert _reopen(True).endswith("Try again shortly.")


def test_the_kind_names_its_own_model_and_host() -> None:
    # The constant the console wires into its coordinator endpoint config.
    from turnstone.console.server import COORDINATOR_MODEL_WORDING

    body = _not_loaded("ghost", requested=False, wording=COORDINATOR_MODEL_WORDING)
    assert body["error"] == (
        "The coordinator model 'ghost' isn't loaded on the console. Ask an admin to "
        "check its definition, or to set coordinator.model_alias."
    )


def test_not_loaded_names_a_requested_model() -> None:
    assert _not_loaded("picked", requested=True)["error"].startswith(
        "The model 'picked' isn't loaded"
    )


def test_not_loaded_echoes_only_an_alias_shaped_name() -> None:
    body = _not_loaded("<script>x</script>", requested=True)
    assert body["alias"] == ""
    assert "<script>" not in body["error"]
    assert body["error"].startswith("The model isn't loaded")


def test_not_loaded_stays_bounded_at_the_echo_cap() -> None:
    # The longest alias echoed (64 characters) with the longest reason, in each
    # kind's wording, create and reopen alike.
    from turnstone.console.server import COORDINATOR_MODEL_WORDING
    from turnstone.core.session_routes import NODE_MODEL_WORDING

    reason = "no enabled model definition by that name is loaded"
    for wording in (NODE_MODEL_WORDING, COORDINATOR_MODEL_WORDING):
        for reopen in (False, True):
            body = _not_loaded(
                "a" * 64, requested=False, reopen=reopen, reason=reason, wording=wording
            )
            assert len(body["error"]) <= 300


def test_not_loaded_with_nothing_configured() -> None:
    assert _not_loaded("", requested=False)["error"].startswith("No default model is set")


# ---------------------------------------------------------------------------
# A fork builds on its source's model when that model is loaded (#1359)
# ---------------------------------------------------------------------------


def _fork_create(
    monkeypatch: Any,
    *,
    source_loaded: bool,
    create_error: Exception,
    supports_resume: bool = True,
) -> tuple[dict[str, Any], Any, Any]:
    """POST a fork create; return the kwargs ``mgr.create`` saw, the response and
    the storage double."""
    from unittest.mock import MagicMock

    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from turnstone.core.session_routes import SessionEndpointConfig, make_create_handler

    storage = MagicMock()
    storage.load_workstream_config.return_value = {"model_alias": "src"}
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    mgr = MagicMock()
    mgr.has_loaded_model.side_effect = lambda alias: source_loaded and alias == "src"
    seen: dict[str, Any] = {}

    def _create(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise create_error

    mgr.create.side_effect = _create
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (mgr, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        create_build_kwargs=lambda _r, body, uid, *_a: {
            "user_id": uid,
            "model": body.get("model") or None,
        },
        create_supports_resume=supports_resume,
    )
    app = Starlette(routes=[Route("/new", make_create_handler(cfg), methods=["POST"])])
    with TestClient(app) as client:
        resp = client.post("/new", json={"resume_ws": "source-ws", "model": "chan"})
    return seen, resp, storage


def test_a_fork_builds_on_its_sources_loaded_model(monkeypatch: Any) -> None:
    # The clone rebinds to the source's model anyway; building on it keeps an
    # unloaded default (or channel model) from failing the fork.
    seen, resp, _storage = _fork_create(
        monkeypatch, source_loaded=True, create_error=RuntimeError("stop")
    )
    assert seen["model"] == "src"
    assert resp.status_code == 429


def test_a_fork_whose_source_model_isnt_loaded_keeps_the_request_model(monkeypatch: Any) -> None:
    seen, _resp, _storage = _fork_create(
        monkeypatch, source_loaded=False, create_error=RuntimeError("stop")
    )
    assert seen["model"] == "chan"


def test_a_fork_refusal_names_the_sources_model(monkeypatch: Any) -> None:
    from turnstone.core.model_registry import UnknownModelAliasError

    _seen, resp, _storage = _fork_create(
        monkeypatch, source_loaded=True, create_error=UnknownModelAliasError("src")
    )
    assert resp.status_code == 503
    assert resp.json()["error"].startswith("The model 'src' isn't loaded on this node")


def test_a_mount_that_doesnt_fork_ignores_resume_ws(monkeypatch: Any) -> None:
    # The coordinator mount checks no source visibility and has no fork, so a
    # resume_ws there must not lend the new workstream the source's model or
    # persona: the source is never read (#1359).
    seen, _resp, storage = _fork_create(
        monkeypatch,
        source_loaded=True,
        create_error=RuntimeError("stop"),
        supports_resume=False,
    )
    assert seen["model"] == "chan"
    storage.load_workstream_config.assert_not_called()
