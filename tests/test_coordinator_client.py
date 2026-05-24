"""Tests for ``turnstone.console.coordinator_client.CoordinatorClient``.

Uses an httpx MockTransport to intercept outbound requests so we verify
the URL map, headers, and body shape without standing up a real console.
Read-op tests hit a real in-memory SQLite backend to confirm the
storage-call path.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from turnstone.console.coordinator_client import (
    _ROUTE_PATHS,
    CoordinatorClient,
    CoordinatorTokenManager,
)
from turnstone.core.auth import JWT_AUD_CONSOLE, validate_jwt
from turnstone.core.child_event_bus import ChildEventBus
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from collections.abc import Callable

_SECRET = "x" * 64


# ---------------------------------------------------------------------------
# CoordinatorTokenManager
# ---------------------------------------------------------------------------


def test_token_manager_mints_valid_console_jwt():
    tm = CoordinatorTokenManager(
        user_id="user-1",
        scopes=frozenset({"read", "write", "approve"}),
        permissions=frozenset({"admin.coordinator"}),
        secret=_SECRET,
        coord_ws_id="coord-123",
        ttl_seconds=300,
    )
    token = tm.token
    result = validate_jwt(token, _SECRET, audience=JWT_AUD_CONSOLE)
    assert result is not None
    assert result.user_id == "user-1"
    assert "approve" in result.scopes
    assert result.token_source == "coordinator"


def test_token_manager_embeds_coord_ws_id_claim():
    import jwt

    tm = CoordinatorTokenManager(
        user_id="user-1",
        scopes=frozenset({"read"}),
        permissions=frozenset(),
        secret=_SECRET,
        coord_ws_id="coord-42",
    )
    token = tm.token
    decoded = jwt.decode(token, _SECRET, algorithms=["HS256"], audience=JWT_AUD_CONSOLE)
    assert decoded["coord_ws_id"] == "coord-42"
    assert decoded["src"] == "coordinator"


def test_token_manager_refreshes_near_expiry(monkeypatch):
    """Force the expiry guard to fire and confirm _mint runs again."""
    tm = CoordinatorTokenManager(
        user_id="u",
        scopes=frozenset({"read"}),
        permissions=frozenset(),
        secret=_SECRET,
        coord_ws_id="c",
        ttl_seconds=10,
    )
    calls = {"count": 0}
    real_mint = tm._mint

    def _counting_mint() -> None:
        calls["count"] += 1
        real_mint()

    monkeypatch.setattr(tm, "_mint", _counting_mint)
    _ = tm.token
    assert calls["count"] == 1
    # Not expired yet → no re-mint.
    _ = tm.token
    assert calls["count"] == 1
    # Force expiry.
    tm._expires_at = 0.0  # type: ignore[attr-defined]
    _ = tm.token
    assert calls["count"] == 2


def test_token_manager_rejects_nonpositive_ttl():
    with pytest.raises(ValueError):
        CoordinatorTokenManager(
            user_id="u",
            scopes=frozenset(),
            permissions=frozenset(),
            secret=_SECRET,
            coord_ws_id="c",
            ttl_seconds=0,
        )


# ---------------------------------------------------------------------------
# CoordinatorClient — URL map + header plumbing via MockTransport
# ---------------------------------------------------------------------------


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[CoordinatorClient, list[httpx.Request]]:
    """Build a CoordinatorClient with an httpx MockTransport recorder.

    Pre-registers the canonical test ws_ids (``ws-x``, ``ws-y``) under
    ``coord-1`` so the client-side tenant guard on send / close / cancel
    / delete passes.  The mutating-op tests want to verify the route
    map + body shape, not the guard.
    """
    captured: list[httpx.Request] = []

    def _trapping(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    transport = httpx.MockTransport(_trapping)
    http = httpx.Client(transport=transport)
    storage = SQLiteBackend(":memory:")
    storage.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    storage.register_workstream(
        "ws-x", kind="interactive", parent_ws_id="coord-1", user_id="user-1"
    )
    storage.register_workstream(
        "ws-y", kind="interactive", parent_ws_id="coord-1", user_id="user-1"
    )
    client = CoordinatorClient(
        console_base_url="http://console",
        storage=storage,
        token_factory=lambda: "test-token",
        coord_ws_id="coord-1",
        user_id="user-1",
        http_client=http,
        child_event_bus=ChildEventBus(),
    )
    return client, captured


def _ok_json(payload: dict) -> Callable[[httpx.Request], httpx.Response]:
    def _h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return _h


def test_route_map_matches_console_routes():
    """URL paths must match what ``turnstone/console/server.py`` registers.

    The routing proxy's _CONSOLE_ROUTES includes:
      POST /v1/api/route/workstreams/new
      POST /v1/api/route/send
      POST /v1/api/route/approve
      POST /v1/api/route/cancel
      POST /v1/api/route/workstreams/close

    Phase B adds /v1/api/route/workstreams/delete; B9 review checks that
    addition lands alongside the others.  Here we assert our internal map
    mirrors the shape we expect.
    """
    assert _ROUTE_PATHS["spawn"] == "/v1/api/route/workstreams/new"
    assert _ROUTE_PATHS["send"] == "/v1/api/route/workstreams/{ws_id}/send"
    assert _ROUTE_PATHS["approve"] == "/v1/api/route/workstreams/{ws_id}/approve"
    assert _ROUTE_PATHS["cancel"] == "/v1/api/route/workstreams/{ws_id}/cancel"
    assert _ROUTE_PATHS["close"] == "/v1/api/route/workstreams/{ws_id}/close"
    # ``delete`` keeps the body-keyed shape — it has its own
    # ``route_workstream_delete`` handler instead of going through
    # the generic route_proxy.
    assert _ROUTE_PATHS["delete"] == "/v1/api/route/workstreams/delete"
    # Cascade endpoint lives on the console itself (not a node), so the
    # path slots in the coord ws_id rather than routing through a proxy.
    assert _ROUTE_PATHS["close_all_children"] == "/v1/api/workstreams/{ws_id}/close_all_children"


def test_route_paths_match_actual_console_mounts():
    """Every entry in ``_ROUTE_PATHS`` must correspond to an actually
    mounted Starlette route on the console app. Catches the kind of
    drift that broke close_workstream / close_all_children when the
    #422 legacy URL adapter removal deleted the body-keyed
    /v1/api/route/{verb} routes without a corresponding update to
    the coord client's route table."""
    from unittest.mock import MagicMock

    from starlette.routing import Mount, Route

    from turnstone.console.coordinator_client import _ROUTE_PATHS
    from turnstone.console.server import create_app

    app = create_app(
        collector=MagicMock(),
        jwt_secret="x" * 64,
    )

    def _walk(routes, prefix=""):
        for r in routes:
            if isinstance(r, Mount):
                yield from _walk(r.routes, prefix=prefix + r.path)
            elif isinstance(r, Route):
                yield prefix + r.path

    mounted = set(_walk(app.routes))

    for key, template in _ROUTE_PATHS.items():
        # Starlette's Route.path uses ``{name}`` placeholders just
        # like our templates, so a literal containment check works.
        assert template in mounted, (
            f"_ROUTE_PATHS[{key!r}] = {template!r} is not a mounted "
            f"console route. Mounted routes containing 'route' or "
            f"'workstreams': "
            f"{sorted(p for p in mounted if 'route' in p or 'workstreams' in p)}"
        )


def test_spawn_posts_to_routing_proxy_with_bearer_token():
    client, captured = _mock_client(_ok_json({"ws_id": "child-1", "name": "c", "node_id": "n1"}))
    result = client.spawn(
        initial_message="hi",
        parent_ws_id="coord-1",
        user_id="user-1",
        skill="my-skill",
        target_node="n1",
    )
    assert result["ws_id"] == "child-1"
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/api/route/workstreams/new"
    assert req.headers["Authorization"] == "Bearer test-token"
    body = json.loads(req.content)
    assert body["kind"] == "interactive"
    assert body["parent_ws_id"] == "coord-1"
    assert body["user_id"] == "user-1"
    assert body["initial_message"] == "hi"
    assert body["skill"] == "my-skill"
    assert body["target_node"] == "n1"


def test_spawn_omits_optional_empty_fields():
    client, captured = _mock_client(_ok_json({"ws_id": "x"}))
    client.spawn(initial_message="hi", parent_ws_id="coord", user_id="u")
    body = json.loads(captured[0].content)
    # Optional fields should NOT be present when empty (keeps body lean
    # and avoids confusing the route proxy's schema).
    assert "skill" not in body
    assert "name" not in body
    assert "model" not in body
    assert "target_node" not in body


def test_send_posts_to_send_route():
    client, captured = _mock_client(_ok_json({"status": 200}))
    client.send("ws-x", "hello")
    # Path-keyed shape post-#422: ws_id rides in the URL, not the body.
    assert captured[0].url.path == "/v1/api/route/workstreams/ws-x/send"
    body = json.loads(captured[0].content)
    assert body == {"message": "hello"}


def test_close_workstream_posts_to_close_route():
    client, captured = _mock_client(_ok_json({"status": 200}))
    client.close_workstream("ws-x")
    assert captured[0].url.path == "/v1/api/route/workstreams/ws-x/close"
    body = json.loads(captured[0].content)
    assert body == {}  # no reason → omitted; ws_id rides the path


def test_close_workstream_includes_reason_when_provided():
    client, captured = _mock_client(_ok_json({"status": 200}))
    client.close_workstream("ws-x", reason="done")
    assert captured[0].url.path == "/v1/api/route/workstreams/ws-x/close"
    body = json.loads(captured[0].content)
    assert body == {"reason": "done"}


def test_close_all_children_posts_to_console_endpoint():
    """Targets the console directly (not the routing proxy).  The URL
    embeds the coord's own ws_id so the server can resolve the session.
    """
    client, captured = _mock_client(
        _ok_json(
            {
                "status": "ok",
                "closed": ["c-1", "c-2"],
                "failed": [],
                "skipped": [],
            }
        )
    )
    result = client.close_all_children(reason="batch done")
    assert result["closed"] == ["c-1", "c-2"]
    assert captured[0].url.path == "/v1/api/workstreams/coord-1/close_all_children"
    assert captured[0].headers["Authorization"] == "Bearer test-token"
    body = json.loads(captured[0].content)
    assert body == {"reason": "batch done"}


def test_close_all_children_omits_empty_reason():
    client, captured = _mock_client(
        _ok_json({"status": "ok", "closed": [], "failed": [], "skipped": []})
    )
    client.close_all_children()
    body = json.loads(captured[0].content)
    assert body == {}


def test_close_all_children_surfaces_http_error():
    def _boom(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    client, _captured = _mock_client(_boom)
    result = client.close_all_children()
    assert result["status"] == 500
    assert "error" in result


def test_close_all_children_surfaces_transport_error():
    def _raise(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client, _captured = _mock_client(_raise)
    result = client.close_all_children()
    assert result["status"] == 0
    assert "upstream unreachable" in result["error"]


def test_delete_workstream_posts_to_delete_route():
    client, captured = _mock_client(_ok_json({"status": 200}))
    client.delete("ws-x")
    assert captured[0].url.path == "/v1/api/route/workstreams/delete"


def test_approve_and_cancel_hit_their_routes():
    client, captured = _mock_client(_ok_json({"status": 200}))
    client.approve("ws-x", call_id="c-1", approved=True, feedback="ok", always=True)
    client.cancel("ws-x")
    # Path-keyed shape post-#422: ws_id rides the URL.
    assert captured[0].url.path == "/v1/api/route/workstreams/ws-x/approve"
    assert captured[1].url.path == "/v1/api/route/workstreams/ws-x/cancel"
    approve_body = json.loads(captured[0].content)
    assert approve_body["approved"] is True
    assert approve_body["always"] is True
    assert approve_body["call_id"] == "c-1"
    # ws_id moved to the URL — make sure we didn't double-encode it.
    assert "ws_id" not in approve_body


def test_http_error_returns_structured_failure():
    def _boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=req)

    client, _captured = _mock_client(_boom)
    result = client.send("ws-x", "hi")
    assert "error" in result
    assert result["status"] == 0


def test_non_2xx_response_populates_error():
    def _h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "upstream down"})

    client, _c = _mock_client(_h)
    result = client.send("ws-x", "hi")
    assert result["status"] == 500
    assert "error" in result


# ---------------------------------------------------------------------------
# Tenant guard — defense in depth on every model-invoked mutating op
# ---------------------------------------------------------------------------


def test_mutating_ops_reject_foreign_ws_id_without_hitting_proxy():
    """A coordinator must not be able to drive a foreign tenant's
    workstream even if the upstream node forgets to enforce ownership.
    Confirm that send / close / cancel / delete short-circuit before
    the HTTP round-trip when the ws_id isn't in the coordinator's own
    subtree.  Same 404-shape that inspect / wait_for_workstream use, so
    the model can't distinguish 'foreign' from 'missing' (no oracle).
    """
    client, captured = _mock_client(_ok_json({"status": 200}))
    # ``ws-foreign`` is not in the coordinator's subtree (the fixture
    # only registers ws-x and ws-y under coord-1).
    for call, kwargs in [
        (client.send, {"message": "hi"}),
        (client.close_workstream, {"reason": "x"}),
        (client.cancel, {}),
        (client.delete, {}),
    ]:
        result = call("ws-foreign", **kwargs)  # type: ignore[arg-type]
        assert result["status"] == 404
        assert "not in coordinator subtree" in result["error"]
    # No HTTP requests issued — guard rejected before _post.
    assert captured == []


def test_mutating_ops_accept_self_ws_id():
    """The coordinator's own ws_id is in its subtree (trivially true);
    operations against self should pass the guard.  Currently only send
    has a meaningful self-targeted use, but the contract should hold
    uniformly."""
    client, captured = _mock_client(_ok_json({"status": 200}))
    client.send("coord-1", "hi")
    assert len(captured) == 1
    assert captured[0].url.path == "/v1/api/route/workstreams/coord-1/send"


# ---------------------------------------------------------------------------
# Read ops — storage-backed
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_storage(tmp_path):
    st = SQLiteBackend(str(tmp_path / "coord.db"))
    # Coord + 2 interactive children + 1 child coordinator (excluded) +
    # 1 unrelated ws + 1 cross-tenant child (excluded by the user_id SQL
    # filter: belongs to user-2 but forged parent_ws_id=coord-1).
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    st.register_workstream(
        "child-a",
        kind="interactive",
        parent_ws_id="coord-1",
        state="idle",
        skill_id="skill-x",
        user_id="user-1",
    )
    st.register_workstream(
        "child-b",
        kind="interactive",
        parent_ws_id="coord-1",
        state="running",
        skill_id="skill-y",
        user_id="user-1",
    )
    st.register_workstream(
        "child-coord",
        kind="coordinator",
        parent_ws_id="coord-1",
        user_id="user-1",
    )
    st.register_workstream("unrelated", kind="interactive", user_id="user-1")
    st.register_workstream(
        "cross-tenant-child",
        kind="interactive",
        parent_ws_id="coord-1",
        user_id="user-2",
    )
    return st


def _make_read_client(storage: SQLiteBackend) -> CoordinatorClient:
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    http = httpx.Client(transport=transport)
    return CoordinatorClient(
        console_base_url="http://x",
        storage=storage,
        token_factory=lambda: "t",
        coord_ws_id="coord-1",
        user_id="user-1",
        http_client=http,
        child_event_bus=ChildEventBus(),
    )


def test_list_children_returns_only_interactive_children(populated_storage):
    client = _make_read_client(populated_storage)
    result = client.list_children("coord-1")
    assert set(result.keys()) == {"children", "truncated"}
    rows = result["children"]
    names = {r["ws_id"] for r in rows}
    # Excludes child-coord (kind filter), unrelated (parent filter),
    # cross-tenant-child (user_id filter).
    assert names == {"child-a", "child-b"}
    for r in rows:
        assert r["kind"] == "interactive"
        assert r["parent_ws_id"] == "coord-1"
    # Well under limit and no filters → not truncated.
    assert result["truncated"] is False


def test_list_children_excludes_cross_tenant_child(populated_storage):
    """SQL-level user_id filter drops forged parent_ws_id rows owned by another user."""
    client = _make_read_client(populated_storage)
    result = client.list_children("coord-1")
    names = {r["ws_id"] for r in result["children"]}
    assert "cross-tenant-child" not in names


def test_list_children_filters_by_state(populated_storage):
    client = _make_read_client(populated_storage)
    result = client.list_children("coord-1", state="running")
    assert {r["ws_id"] for r in result["children"]} == {"child-b"}


def test_list_children_filters_by_skill_id(populated_storage):
    client = _make_read_client(populated_storage)
    result = client.list_children("coord-1", skill="skill-x")
    rows = result["children"]
    assert {r["ws_id"] for r in rows} == {"child-a"}
    assert rows[0].get("skill_id") == "skill-x"


def test_list_children_skill_filter_avoids_n_plus_one(populated_storage, monkeypatch):
    """skill filter must read skill_id/skill_version from the list_workstreams
    projection — no per-row get_workstream round-trip (Copilot review #7)."""
    client = _make_read_client(populated_storage)
    call_count = {"n": 0}
    real_get = populated_storage.get_workstream

    def _counting_get(ws_id: str):
        call_count["n"] += 1
        return real_get(ws_id)

    monkeypatch.setattr(populated_storage, "get_workstream", _counting_get)
    result = client.list_children("coord-1", skill="skill-x")
    assert {r["ws_id"] for r in result["children"]} == {"child-a"}
    assert result["children"][0]["skill_id"] == "skill-x"
    assert call_count["n"] == 0


def test_list_children_signals_truncation_when_page_full_and_filter_drops(
    populated_storage,
):
    """limit=1 with a state filter that drops the fetched row should
    flag truncated=True so the model knows more may exist."""
    client = _make_read_client(populated_storage)
    # populated_storage has child-a (idle) and child-b (running) under
    # coord-1.  limit=1 + state=running may return child-a first then
    # drop it -> truncated=True.  Either order, the row-budget is
    # exhausted before all matches are considered.
    result = client.list_children("coord-1", state="running", limit=1)
    # If the fetched row happens to match, truncated is False; otherwise
    # True.  Either way, the dict shape is stable.
    assert "truncated" in result
    assert isinstance(result["truncated"], bool)


def test_inspect_missing_ws_returns_error(populated_storage):
    client = _make_read_client(populated_storage)
    result = client.inspect("does-not-exist")
    assert "error" in result


def test_inspect_not_found_does_not_echo_ws_id_in_error_string(populated_storage):
    """The error STRING is bare ("workstream not found") — the
    structured ``ws_id`` field carries the queried id.  Pre-fix the
    error message echoed the ws_id back at the caller who just sent
    it, which was redundant and a stylistic departure from the rest
    of the surface.  Echo-in-string is also one more place a
    hostile/oversize ws_id could land in operator-facing text."""
    client = _make_read_client(populated_storage)
    result = client.inspect("does-not-exist-xyz")
    assert result["error"] == "workstream not found"
    # The structured field still carries the ws_id for context.
    assert result["ws_id"] == "does-not-exist-xyz"


def test_inspect_cross_tenant_returns_same_shape_as_missing(populated_storage):
    """The cross-tenant guard MUST return the exact same shape as a
    genuinely missing ws_id — that's the existence-leak defence the
    error-string echo was carrying weight for too.  Asserting the
    shape match here pins the property going forward."""
    # ``unrelated`` exists in storage but is not a coord-1 child.
    client = _make_read_client(populated_storage)
    cross_tenant = client.inspect("unrelated")
    missing = client.inspect("does-not-exist-abc")
    # Same key set, same error string, only the ws_id field differs.
    assert cross_tenant.keys() == missing.keys()
    assert cross_tenant["error"] == missing["error"] == "workstream not found"
    assert cross_tenant["ws_id"] == "unrelated"
    assert missing["ws_id"] == "does-not-exist-abc"


def test_list_children_excludes_closed_by_default(tmp_path):
    """Default ``list_children`` filters out closed / deleted rows —
    the common "what's still running?" query shouldn't have to
    post-hoc filter them.  An explicit state filter still wins."""
    st = SQLiteBackend(str(tmp_path / "closed.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    st.register_workstream(
        "child-active",
        kind="interactive",
        parent_ws_id="coord-1",
        state="idle",
        user_id="user-1",
    )
    st.register_workstream(
        "child-closed",
        kind="interactive",
        parent_ws_id="coord-1",
        state="closed",
        user_id="user-1",
    )
    st.register_workstream(
        "child-deleted",
        kind="interactive",
        parent_ws_id="coord-1",
        state="deleted",
        user_id="user-1",
    )
    client = _make_read_client(st)
    result = client.list_children("coord-1")
    ids = {c["ws_id"] for c in result["children"]}
    assert ids == {"child-active"}
    # Opt-in surfaces everything.
    with_closed = client.list_children("coord-1", include_closed=True)
    all_ids = {c["ws_id"] for c in with_closed["children"]}
    assert all_ids == {"child-active", "child-closed", "child-deleted"}
    # Explicit state=closed overrides the default-exclude.
    closed_only = client.list_children("coord-1", state="closed")
    closed_ids = {c["ws_id"] for c in closed_only["children"]}
    assert closed_ids == {"child-closed"}


def test_inspect_returns_persisted_fields(populated_storage):
    client = _make_read_client(populated_storage)
    result = client.inspect("child-a")
    # Core persisted fields
    for key in ("ws_id", "state", "kind", "parent_ws_id", "user_id", "created", "updated"):
        assert key in result
    assert result["parent_ws_id"] == "coord-1"
    assert isinstance(result["messages"], list)
    # Verdicts deliberately not surfaced — see the inline comment in
    # CoordinatorClient.inspect().
    assert "verdicts" not in result


def test_inspect_refuses_workstreams_outside_coordinator_subtree(populated_storage):
    """Prompt-injection guard — coordinator must not be able to inspect
    arbitrary ws_ids (e.g. another tenant's workstream)."""
    client = _make_read_client(populated_storage)
    # 'unrelated' has no parent_ws_id and is not coord-1 itself.
    result = client.inspect("unrelated")
    assert "error" in result
    assert "messages" not in result


def _make_client_with_cluster_response(
    storage: SQLiteBackend, status: int, body: dict[str, Any] | None = None
) -> CoordinatorClient:
    """Build a CoordinatorClient whose mocked HTTP transport returns
    ``status`` + ``body`` for any ``/cluster/ws/.../detail`` GET."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if "/v1/api/cluster/ws/" in request.url.path and request.method == "GET":
            return httpx.Response(status, json=body or {})
        return httpx.Response(200, json={})

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    return CoordinatorClient(
        console_base_url="http://x",
        storage=storage,
        token_factory=lambda: "t",
        coord_ws_id="coord-1",
        user_id="user-1",
        http_client=http,
        child_event_bus=ChildEventBus(),
    )


def test_inspect_merges_live_block_when_cluster_endpoint_returns_200(populated_storage):
    """Creator has admin.cluster.inspect → cluster endpoint returns
    live state → inspect() merges `live` onto the storage snapshot."""
    live_payload = {
        "persisted": {"ws_id": "child-a"},
        "live": {
            "state": "running",
            "tokens": 42,
            "activity": "bash ls",
            "activity_state": "tool",
            "pending_approval": False,
        },
        "messages": [],
    }
    client = _make_client_with_cluster_response(populated_storage, status=200, body=live_payload)
    result = client.inspect("child-a")
    assert "live" in result
    assert result["live"]["state"] == "running"
    assert result["live"]["tokens"] == 42


def test_inspect_degrades_to_storage_only_on_cluster_endpoint_403(populated_storage):
    """Creator lacks admin.cluster.inspect → cluster endpoint returns
    403 → inspect() falls back to storage-only with no `live` key.

    This documents the permission-inheritance contract: the coordinator
    cannot see more than its creator, so a 403 at the live endpoint is
    expected behavior for users without the opt-in permission."""
    client = _make_client_with_cluster_response(
        populated_storage, status=403, body={"error": "forbidden"}
    )
    result = client.inspect("child-a")
    assert "live" not in result
    # Storage fields still present.
    assert result["ws_id"] == "child-a"


def test_inspect_degrades_to_storage_only_on_cluster_endpoint_503(populated_storage):
    """Live-state endpoint can transiently fail (node unreachable,
    timeout, 5xx) — same degrade path."""
    client = _make_client_with_cluster_response(
        populated_storage, status=503, body={"error": "node unreachable"}
    )
    result = client.inspect("child-a")
    assert "live" not in result
    assert result["ws_id"] == "child-a"


def test_list_children_refuses_arbitrary_parent_ws_id(populated_storage):
    """Prompt-injection guard — coordinator must not be able to enumerate
    children of some other coordinator."""
    # Add a sibling coordinator with its own children.
    populated_storage.register_workstream(
        "coord-other",
        kind="coordinator",
        user_id="user-2",
    )
    populated_storage.register_workstream(
        "child-other",
        kind="interactive",
        parent_ws_id="coord-other",
    )
    client = _make_read_client(populated_storage)
    result = client.list_children("coord-other")
    assert result == {"children": [], "truncated": False}


def test_list_children_truncated_signals_db_page_full(populated_storage):
    """truncated=True whenever the SQL fetch hit the limit, regardless
    of post-filtering."""
    client = _make_read_client(populated_storage)
    # populated_storage has child-a + child-b under coord-1; limit=1
    # always fills the page so truncated must fire.
    result = client.list_children("coord-1", limit=1)
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# list_nodes
# ---------------------------------------------------------------------------


def _set_meta(storage, node_id, entries):
    """Write node metadata the way production writers do — JSON-encoded values.

    ``server.py``, ``admin.py``, and ``console/server.py`` all call
    ``set_node_metadata[_bulk]`` with ``json.dumps(value)``.  Tests have
    to use the same encoding so coordinator filter semantics are
    validated against realistic data.
    """
    storage.set_node_metadata_bulk(
        node_id,
        [(k, json.dumps(v), src) for (k, v, src) in entries],
    )


def _register_service(storage, node_id: str, url: str = "http://x:8080") -> None:
    """Register a node in the services table so list_nodes' liveness
    filter treats it as active (recent heartbeat)."""
    storage.register_service("server", node_id, url)


@pytest.fixture
def storage_with_nodes(tmp_path):
    st = SQLiteBackend(str(tmp_path / "nodes.db"))
    _set_meta(
        st,
        "node-a",
        [
            ("arch", "x86_64", "auto"),
            ("cpu_count", 4, "auto"),
            ("region", "us-east", "user"),
        ],
    )
    _register_service(st, "node-a")
    _set_meta(
        st,
        "node-b",
        [
            ("arch", "x86_64", "auto"),
            ("cpu_count", 16, "auto"),
            ("region", "us-west", "user"),
            ("capability", "gpu", "user"),
        ],
    )
    _register_service(st, "node-b")
    _set_meta(
        st,
        "node-c",
        [("arch", "arm64", "auto"), ("cpu_count", 8, "auto")],
    )
    _register_service(st, "node-c")
    return st


def test_list_nodes_no_filters_returns_all_rows_decoded(storage_with_nodes):
    client = _make_read_client(storage_with_nodes)
    result = client.list_nodes()
    assert set(result.keys()) == {"nodes", "truncated"}
    node_ids = {n["node_id"] for n in result["nodes"]}
    assert node_ids == {"node-a", "node-b", "node-c"}
    assert result["truncated"] is False
    # Values round-trip through json.loads — model sees natural types,
    # not the raw stored JSON text.
    node_b = next(n for n in result["nodes"] if n["node_id"] == "node-b")
    assert node_b["metadata"]["arch"] == {"value": "x86_64", "source": "auto"}
    assert node_b["metadata"]["cpu_count"] == {"value": 16, "source": "auto"}
    assert node_b["metadata"]["capability"] == {"value": "gpu", "source": "user"}


def test_list_nodes_strips_interfaces_by_default(tmp_path):
    """The auto-populated ``interfaces`` key carries internal RFC 1918
    addresses which trip the private_ip_disclosure output guard and
    aren't used for routing decisions.  Default response omits it."""
    st = SQLiteBackend(str(tmp_path / "nodes.db"))
    _set_meta(
        st,
        "node-x",
        [
            ("arch", "x86_64", "auto"),
            ("interfaces", {"eth0": ["172.18.0.4"]}, "auto"),
            ("region", "us-east", "user"),
        ],
    )
    _register_service(st, "node-x")
    client = _make_read_client(st)
    result = client.list_nodes()
    node = result["nodes"][0]
    assert "interfaces" not in node["metadata"]
    # Other auto keys still land.
    assert "arch" in node["metadata"]
    assert "region" in node["metadata"]


def test_list_nodes_include_network_detail_opt_in(tmp_path):
    """Operators who need the IP map for debugging opt back in."""
    st = SQLiteBackend(str(tmp_path / "nodes.db"))
    _set_meta(
        st,
        "node-x",
        [
            ("arch", "x86_64", "auto"),
            ("interfaces", {"eth0": ["172.18.0.4"]}, "auto"),
        ],
    )
    _register_service(st, "node-x")
    client = _make_read_client(st)
    result = client.list_nodes(include_network_detail=True)
    node = result["nodes"][0]
    assert "interfaces" in node["metadata"]
    assert node["metadata"]["interfaces"]["value"] == {"eth0": ["172.18.0.4"]}


def test_list_nodes_filters_stale_registrations_by_default(tmp_path):
    """node_metadata rows persist across restarts but the services
    table heartbeats expire — list_nodes should intersect against
    active services so the model doesn't suggest a dead node for
    target_node pinning.  Regression for the stale-registration bug."""
    st = SQLiteBackend(str(tmp_path / "nodes.db"))
    _set_meta(st, "node-live", [("arch", "x86_64", "auto")])
    _set_meta(st, "node-dead", [("arch", "x86_64", "auto")])
    # Only node-live has a fresh heartbeat; node-dead is metadata-only.
    _register_service(st, "node-live")
    client = _make_read_client(st)
    result = client.list_nodes()
    ids = {n["node_id"] for n in result["nodes"]}
    assert ids == {"node-live"}
    # Opt-in surfaces the stale registration for troubleshooting.
    full = client.list_nodes(include_inactive=True)
    full_ids = {n["node_id"] for n in full["nodes"]}
    assert full_ids == {"node-live", "node-dead"}


def test_list_nodes_filter_uses_natural_value_not_quoted(storage_with_nodes, monkeypatch):
    """Model passes ``{"capability": "gpu"}`` — client re-encodes to
    ``'"gpu"'`` before filter_nodes_by_metadata so the stored text
    matches.  Also asserts the filtered path fetches metadata only for
    the paginated slice (bounded at page_size) rather than the whole
    cluster — no wide ``get_all_node_metadata`` scan on a narrow filter.
    """
    per_node_calls: list[str] = []
    real = storage_with_nodes.get_node_metadata

    def _spy(nid):  # type: ignore[no-untyped-def]
        per_node_calls.append(nid)
        return real(nid)

    all_meta_calls: list[int] = []
    real_all = storage_with_nodes.get_all_node_metadata

    def _spy_all():  # type: ignore[no-untyped-def]
        all_meta_calls.append(1)
        return real_all()

    monkeypatch.setattr(storage_with_nodes, "get_node_metadata", _spy)
    monkeypatch.setattr(storage_with_nodes, "get_all_node_metadata", _spy_all)

    client = _make_read_client(storage_with_nodes)
    result = client.list_nodes(filters={"capability": "gpu"})
    assert {n["node_id"] for n in result["nodes"]} == {"node-b"}
    # Filtered path: no wide scan; per-node lookups bounded to the
    # matching page (1 row matched the filter).
    assert all_meta_calls == []
    assert per_node_calls == ["node-b"]


def test_list_nodes_filter_accepts_int_and_encodes_correctly(storage_with_nodes):
    """Model passes ``{"cpu_count": 4}`` — int encoded to ``"4"``; match."""
    client = _make_read_client(storage_with_nodes)
    result = client.list_nodes(filters={"cpu_count": 4})
    assert {n["node_id"] for n in result["nodes"]} == {"node-a"}


def test_list_nodes_int_and_string_filters_are_distinct(storage_with_nodes):
    """The JSON schema for ``filters`` accepts primitives (string, integer,
    number, boolean); stringified ints compare as strings, not as ints.
    The tool description documents this as ``JSON-equal compare``.
    """
    client = _make_read_client(storage_with_nodes)
    # Int filter against int-stored value matches.
    assert {n["node_id"] for n in client.list_nodes(filters={"cpu_count": 4})["nodes"]} == {
        "node-a"
    }
    # String filter against int-stored value is a distinct comparison and
    # returns zero rows — ``"4"`` JSON-encodes to ``'"4"'`` but the stored
    # row is ``'4'``.  Documented in the tool description.
    assert client.list_nodes(filters={"cpu_count": "4"})["nodes"] == []


def test_list_nodes_truncation_signal(storage_with_nodes):
    client = _make_read_client(storage_with_nodes)
    result = client.list_nodes(limit=2)
    assert len(result["nodes"]) == 2
    assert result["truncated"] is True


def test_list_nodes_empty_on_no_matching_filters(storage_with_nodes):
    client = _make_read_client(storage_with_nodes)
    result = client.list_nodes(filters={"region": "nowhere"})
    assert result["nodes"] == []
    assert result["truncated"] is False


def test_list_nodes_surfaces_healthy_model_aliases(tmp_path):
    """The node's heartbeat loop projects its registry into a ``models``
    metadata entry shaped like ``[{alias, provider, healthy}, ...]``.
    ``list_nodes`` flattens that to the healthy-alias list at the top
    level (under ``model_aliases``) so a coordinator can pass aliases
    straight to ``spawn_workstream(model=)`` without having to
    introspect the metadata blob.  The provider-side model identifier
    (``cfg.model``) is intentionally NOT in the payload — coords kept
    reaching for it when they should pass the local alias."""
    st = SQLiteBackend(str(tmp_path / "nodes.db"))
    _set_meta(
        st,
        "node-x",
        [
            ("arch", "x86_64", "auto"),
            (
                "models",
                [
                    {"alias": "gpt5", "provider": "openai", "healthy": True},
                    {"alias": "claude-opus-47", "provider": "anthropic", "healthy": True},
                    {"alias": "broken", "provider": "openai", "healthy": False},
                ],
                "auto",
            ),
        ],
    )
    _register_service(st, "node-x")
    client = _make_read_client(st)
    result = client.list_nodes()
    node = result["nodes"][0]
    assert node["model_aliases"] == ["gpt5", "claude-opus-47"]
    # Full per-alias info still available under metadata for callers
    # that want provider / healthy detail (e.g. surfacing degraded
    # aliases in a UI).
    full = node["metadata"]["models"]["value"]
    assert {row["alias"] for row in full} == {"gpt5", "claude-opus-47", "broken"}
    # ``model`` (the provider-side identifier) is intentionally absent
    # — keep the payload to the three values a coord actually uses.
    for row in full:
        assert "model" not in row


def test_list_nodes_model_aliases_distinct_from_metadata_models(tmp_path):
    """Pin the naming distinction explicitly: the top-level shortlist
    (``model_aliases``, list of strings) and the rich metadata blob
    (``metadata.models.value``, list of dicts) live under different
    keys so a caller that confuses them gets a clear KeyError rather
    than a silent shape mismatch."""
    st = SQLiteBackend(str(tmp_path / "nodes.db"))
    _set_meta(
        st,
        "node-x",
        [
            (
                "models",
                [{"alias": "a", "provider": "openai", "healthy": True}],
                "auto",
            ),
        ],
    )
    _register_service(st, "node-x")
    client = _make_read_client(st)
    node = client.list_nodes()["nodes"][0]
    # No top-level ``models`` field — only ``model_aliases``.
    assert "models" not in node
    assert node["model_aliases"] == ["a"]
    # Rich shape stays under metadata.
    assert isinstance(node["metadata"]["models"]["value"], list)
    assert isinstance(node["metadata"]["models"]["value"][0], dict)


def test_list_nodes_model_aliases_empty_when_node_has_not_published(tmp_path):
    """Nodes from older builds — or a node mid-startup before its first
    metadata write — won't have a ``models`` entry.  The top-level
    ``model_aliases`` field defaults to ``[]`` rather than being
    omitted so coordinators can rely on the key being present."""
    st = SQLiteBackend(str(tmp_path / "nodes.db"))
    _set_meta(st, "node-y", [("arch", "x86_64", "auto")])
    _register_service(st, "node-y")
    client = _make_read_client(st)
    result = client.list_nodes()
    assert result["nodes"][0]["model_aliases"] == []


def test_list_nodes_models_tolerates_malformed_entries(tmp_path):
    """If a node ever stores a malformed ``models`` entry (wrong outer
    type, missing alias, non-bool healthy), the projection drops the
    bad rows rather than raising — the rest of the response should
    still be useful."""
    st = SQLiteBackend(str(tmp_path / "nodes.db"))
    _set_meta(
        st,
        "node-z",
        [
            (
                "models",
                [
                    {"alias": "ok", "provider": "p", "healthy": True},
                    "not-a-dict",
                    {"provider": "p", "healthy": True},  # missing alias
                    {"alias": "", "healthy": True},  # empty alias
                    {"alias": "degraded", "healthy": False},
                    {"alias": 42, "healthy": True},  # non-string alias
                ],
                "auto",
            ),
        ],
    )
    _register_service(st, "node-z")
    client = _make_read_client(st)
    result = client.list_nodes()
    assert result["nodes"][0]["model_aliases"] == ["ok"]


def test_list_nodes_models_handles_non_list_payload(tmp_path):
    """A node with a corrupted models entry (dict, scalar, null) shouldn't
    blow up the whole list_nodes call.  ``model_aliases`` falls back to ``[]``."""
    st = SQLiteBackend(str(tmp_path / "nodes.db"))
    _set_meta(
        st,
        "node-w",
        [
            ("models", {"oops": "not a list"}, "auto"),
        ],
    )
    _register_service(st, "node-w")
    client = _make_read_client(st)
    result = client.list_nodes()
    assert result["nodes"][0]["model_aliases"] == []


# ---------------------------------------------------------------------------
# inspect — close_reason + token fallback
# ---------------------------------------------------------------------------


def test_inspect_surfaces_close_reason_when_persisted(populated_storage):
    """Operator-supplied close reason is persisted to workstream_config
    by the server's close handler and surfaced by inspect for terminal
    workstreams (closed/error/deleted).  Live workstreams skip the
    config read on the hot path."""
    populated_storage.update_workstream_state("child-a", "closed")
    populated_storage.save_workstream_config("child-a", {"close_reason": "task complete"})
    client = _make_read_client(populated_storage)
    result = client.inspect("child-a")
    assert result.get("close_reason") == "task complete"


def test_inspect_omits_close_reason_when_absent(populated_storage):
    populated_storage.update_workstream_state("child-a", "closed")
    client = _make_read_client(populated_storage)
    result = client.inspect("child-a")
    assert "close_reason" not in result


def test_inspect_surfaces_last_error_when_state_is_error(populated_storage):
    """A child that crashed (e.g. provider 4xx after retry exhaustion)
    has its exception text persisted to workstream_config.last_error
    by the worker-thread error path; inspect surfaces it for terminal
    error rows so the coordinator can triage without parsing the
    assistant tail."""
    populated_storage.update_workstream_state("child-a", "error")
    populated_storage.save_workstream_config(
        "child-a",
        {"last_error": "AuthenticationError: invalid api key"},
    )
    client = _make_read_client(populated_storage)
    result = client.inspect("child-a")
    assert result.get("last_error") == "AuthenticationError: invalid api key"


def test_inspect_omits_last_error_for_non_error_terminal_states(populated_storage):
    """A historic last_error from an earlier failed turn that was later
    closed cleanly must NOT surface on the close — the coord would
    misread the close as an error close.  Gating on state=='error'
    keeps the surface honest."""
    populated_storage.update_workstream_state("child-a", "closed")
    populated_storage.save_workstream_config(
        "child-a",
        {"last_error": "stale error from a previous failed turn"},
    )
    client = _make_read_client(populated_storage)
    result = client.inspect("child-a")
    assert "last_error" not in result


def test_inspect_skips_workstream_config_read_for_live_workstreams(populated_storage, monkeypatch):
    """Hot-path optimisation: live (non-terminal) workstreams must NOT
    pay the per-inspect load_workstream_config round-trip.  close_reason
    can only be set via the server's close handler, so reading the
    config row for a still-running child is pure waste."""
    calls: list[str] = []
    real = populated_storage.load_workstream_config

    def _spy(ws_id: str):  # type: ignore[no-untyped-def]
        calls.append(ws_id)
        return real(ws_id)

    monkeypatch.setattr(populated_storage, "load_workstream_config", _spy)
    client = _make_read_client(populated_storage)
    # child-a is idle (per the populated_storage fixture) — non-terminal.
    client.inspect("child-a")
    assert calls == []


def test_inspect_live_falls_back_to_persisted_tokens(populated_storage):
    """live block carries tokens=0 for an idle child whose node hasn't
    published a fresh tick — fall back to SUM(usage_events) so the
    coordinator doesn't read 0 for a child that already burned tokens."""
    populated_storage.record_usage_event(
        event_id="ev1",
        ws_id="child-a",
        prompt_tokens=100,
        completion_tokens=50,
    )
    populated_storage.record_usage_event(
        event_id="ev2",
        ws_id="child-a",
        prompt_tokens=200,
        completion_tokens=80,
    )
    client = _make_client_with_cluster_response(
        populated_storage,
        status=200,
        body={"persisted": {"ws_id": "child-a"}, "live": {"state": "idle", "tokens": 0}},
    )
    result = client.inspect("child-a")
    assert result["live"]["tokens"] == 100 + 50 + 200 + 80


def test_inspect_live_keeps_nonzero_live_tokens(populated_storage):
    """When the live counter is non-zero, the persisted aggregate is
    NOT consulted — live wins for in-flight workstreams."""
    populated_storage.record_usage_event(
        event_id="ev1",
        ws_id="child-a",
        prompt_tokens=999,
        completion_tokens=999,
    )
    client = _make_client_with_cluster_response(
        populated_storage,
        status=200,
        body={
            "persisted": {"ws_id": "child-a"},
            "live": {"state": "running", "tokens": 17},
        },
    )
    result = client.inspect("child-a")
    assert result["live"]["tokens"] == 17


# ---------------------------------------------------------------------------
# wait_for_workstream
# ---------------------------------------------------------------------------


def test_wait_for_workstream_returns_immediately_when_already_terminal(
    populated_storage,
):
    """Idle / closed children must not block — wait returns at once."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    assert result["complete"] is True
    assert result["mode"] == "any"
    assert result["results"]["child-a"]["state"] == "idle"
    # Must finish in well under the requested timeout.
    assert result["elapsed"] < 1.0


def test_wait_for_workstream_any_mode_returns_when_first_terminal(
    populated_storage,
):
    """child-a is idle (terminal), child-b is running (non-terminal) —
    mode='any' should return without blocking on child-b."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-b", "child-a"], timeout=5, mode="any")
    assert result["complete"] is True
    assert result["results"]["child-a"]["state"] == "idle"
    assert result["results"]["child-b"]["state"] == "running"
    assert result["elapsed"] < 1.0


def test_wait_for_workstream_all_mode_times_out_on_running_child(populated_storage):
    """child-b stays running indefinitely — mode='all' must hit timeout
    rather than block forever."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a", "child-b"], timeout=1.0, mode="all")
    assert result["complete"] is False
    assert result["elapsed"] >= 1.0
    # Both states still observed.
    assert result["results"]["child-a"]["state"] == "idle"
    assert result["results"]["child-b"]["state"] == "running"


def test_wait_for_workstream_denies_foreign_ws_id(populated_storage):
    """A ws_id outside the coordinator's subtree returns state='denied'.
    With mode='any' on a pure-denied list there's no real work to wait
    for, so the wait short-circuits sub-second with complete=False —
    the model sees the denied state immediately and can correct rather
    than spinning the timeout."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["unrelated"], timeout=5, mode="any")
    assert result["results"]["unrelated"]["state"] == "denied"
    assert result["complete"] is False
    assert result["elapsed"] < 1.0


def test_wait_for_workstream_denies_cross_tenant_child(populated_storage):
    """Defense-in-depth (Copilot #506): a row whose ``parent_ws_id``
    matches the coordinator but whose ``user_id`` belongs to a
    different tenant must collapse to ``denied`` — otherwise a
    forged / migration-era / pre-tenant-gate row would let a
    coordinator's LLM observe foreign-tenant state through
    ``wait_for_workstream``.  The ``populated_storage`` fixture's
    ``cross-tenant-child`` row has exactly this shape
    (parent_ws_id="coord-1", user_id="user-2").
    """
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["cross-tenant-child"], timeout=5, mode="any")
    assert result["results"]["cross-tenant-child"]["state"] == "denied"
    assert result["complete"] is False
    assert result["elapsed"] < 1.0


def test_wait_for_workstream_missing_ws_id_indistinguishable_from_denied(populated_storage):
    """A ws_id that doesn't exist collapses into the same 'denied'
    shape as a foreign ws_id so wait can't be used as an existence
    oracle (matches the 404-mask contract inspect uses).  Same
    short-circuit semantics as the pure-foreign case."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["does-not-exist"], timeout=5, mode="any")
    assert result["results"]["does-not-exist"]["state"] == "denied"
    assert result["complete"] is False
    assert result["elapsed"] < 1.0


def test_wait_for_workstream_any_does_not_short_circuit_on_mixed_denied(populated_storage):
    """Regression for the bug-2 false-positive: mode='any' with one
    real (running) child and one denied id must NOT return
    complete=True on the denied id — wait until the real child reaches
    a real terminal state, or time out."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-b", "unrelated"], timeout=1.0, mode="any")
    # child-b never reaches terminal in the test fixture; denied alone
    # must not satisfy the any condition; wait must hit the timeout.
    assert result["complete"] is False
    assert result["elapsed"] >= 1.0
    assert result["results"]["unrelated"]["state"] == "denied"
    assert result["results"]["child-b"]["state"] == "running"


def test_wait_for_workstream_all_completes_when_real_terminal_and_denied_mixed(
    populated_storage,
):
    """mode='all' should consider denied ids as 'settled' so a wait on
    [real-idle, denied] completes after the first tick instead of
    waiting out the timeout — the model gets the full results dict
    and can act on the per-id state."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a", "unrelated"], timeout=5, mode="all")
    assert result["complete"] is True
    assert result["elapsed"] < 1.0
    assert result["results"]["child-a"]["state"] == "idle"
    assert result["results"]["unrelated"]["state"] == "denied"


def test_wait_for_workstream_rejects_invalid_mode(populated_storage):
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], mode="bogus")
    assert "error" in result
    assert result["complete"] is False


def test_wait_for_workstream_rejects_empty_ws_ids(populated_storage):
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream([], timeout=5)
    assert "error" in result


def test_wait_for_workstream_rejects_overflow(populated_storage):
    """Overflow returns an explicit error rather than silently truncating —
    a mode='all' wait that polled only the first cap entries would have
    returned complete=True with N>cap dropped ids never tracked."""
    client = _make_read_client(populated_storage)
    huge = [f"phantom-{i}" for i in range(CoordinatorClient._WAIT_MAX_WS_IDS + 5)]
    result = client.wait_for_workstream(huge, timeout=5, mode="any")
    assert "error" in result
    assert "too many ws_ids" in result["error"]
    assert result["complete"] is False


def test_wait_for_workstream_caps_timeout(populated_storage):
    """timeout > _WAIT_MAX_TIMEOUT clamps silently — an oversized
    timeout is benign (caller can wait less than they asked) so it
    doesn't deserve an explicit error."""
    client = _make_read_client(populated_storage)
    # child-a is already terminal, so the wait completes before any
    # clamped timeout matters; just verify the call doesn't error.
    result = client.wait_for_workstream(["child-a"], timeout=9999, mode="any")
    assert "error" not in result
    assert result["complete"] is True


def test_wait_for_workstream_dedupes_ws_ids(populated_storage):
    """Duplicate ids collapse before polling so the resolved-count
    denominator and the polled set agree."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a", "child-a", "child-a"], timeout=5, mode="any")
    assert "error" not in result
    assert list(result["results"].keys()) == ["child-a"]


def test_wait_for_workstream_never_falls_back_to_per_id_storage_calls(
    populated_storage, monkeypatch
):
    """All storage reads issued by ``wait_for_workstream`` must go
    through the batched paths.  At the documented cap (32 ws_ids over
    a 600 s wait) the naive per-id shape produced ~38k row reads, so
    a regression to per-id is the meaningful failure mode this test
    guards against.

    The primary safety net is the ``pytest.fail`` mock on the per-id
    ``get_workstream`` / ``sum_workstream_tokens`` paths — any call
    there blows up loudly with the regression message.  The
    additional ``batch_calls`` / ``sum_calls`` assertions cover the
    subtler regression where the call IS batched but only covers a
    subset of ws_ids (e.g. one ws_id per call in a loop).
    """
    client = _make_read_client(populated_storage)
    batch_calls: list[list[str]] = []
    sum_calls: list[list[str]] = []
    real_get_batch = populated_storage.get_workstreams_batch
    real_sum_batch = populated_storage.sum_workstream_tokens_batch

    def _spy_get(ws_ids):  # type: ignore[no-untyped-def]
        batch_calls.append(list(ws_ids))
        return real_get_batch(ws_ids)

    def _spy_sum(ws_ids):  # type: ignore[no-untyped-def]
        sum_calls.append(list(ws_ids))
        return real_sum_batch(ws_ids)

    monkeypatch.setattr(populated_storage, "get_workstreams_batch", _spy_get)
    monkeypatch.setattr(populated_storage, "sum_workstream_tokens_batch", _spy_sum)
    # Fail loudly if anything still calls the non-batched paths.
    monkeypatch.setattr(
        populated_storage,
        "get_workstream",
        lambda *a, **kw: pytest.fail("wait_for_workstream must use batched get"),
    )
    monkeypatch.setattr(
        populated_storage,
        "sum_workstream_tokens",
        lambda *a, **kw: pytest.fail("wait_for_workstream must use batched sum"),
    )

    result = client.wait_for_workstream(["child-a", "child-b"], timeout=5, mode="any")
    assert result["complete"] is True
    # Every batched call carried the full ws_id set.  The exact count
    # (currently 2: one pre-loop ownership filter + one snapshot tick)
    # is incidental; if either gains another batched read it stays
    # batched, which is the property under test.
    assert batch_calls, "no batched get_workstreams_batch call observed"
    assert sum_calls, "no batched sum_workstream_tokens_batch call observed"
    first_batch = set(batch_calls[0])
    first_sum = set(sum_calls[0])
    assert first_batch == {"child-a", "child-b"}
    assert first_sum == {"child-a", "child-b"}


def test_wait_for_workstream_handles_non_string_mode(populated_storage):
    """A model that emits ``mode=123`` or ``mode=['any']`` produces a
    clean error rather than crashing with AttributeError on .strip()."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], mode=123)  # type: ignore[arg-type]
    assert "error" in result
    assert "invalid mode" in result["error"]


# ---------------------------------------------------------------------------
# wait_for_workstream — event-driven (ChildEventBus wired in)
# ---------------------------------------------------------------------------
#
# When the coord adapter wires its ``child_event_bus`` into the client,
# the wait loop blocks on a per-call ``threading.Event`` keyed by ws_id
# and only re-snapshots storage on state-change wakes or the heartbeat
# cap.  The legacy ``time.sleep`` poll path remains intact for tests
# that don't wire the bus (above), so this section adds focused
# coverage of the bus-driven behaviour without re-running the full
# matrix of mode / since / cross-tenant cases.


def _make_read_client_with_bus(storage, bus) -> CoordinatorClient:
    """Like ``_make_read_client`` but wires a real ``ChildEventBus``.

    Caller owns the bus so the test can call ``bus.notify(ws_id)`` to
    simulate the dispatch-sink wake-up.
    """
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    http = httpx.Client(transport=transport)
    return CoordinatorClient(
        console_base_url="http://x",
        storage=storage,
        token_factory=lambda: "t",
        coord_ws_id="coord-1",
        user_id="user-1",
        http_client=http,
        child_event_bus=bus,
    )


def test_wait_with_bus_returns_immediately_when_already_terminal(populated_storage):
    """Subscribe-after-terminal race: the wait registers its waiter
    BEFORE the first snapshot, then re-snapshots — an already-terminal
    child must return at once without spinning the heartbeat cap.
    """
    from turnstone.core.child_event_bus import ChildEventBus

    bus = ChildEventBus()
    client = _make_read_client_with_bus(populated_storage, bus)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    assert result["complete"] is True
    assert result["results"]["child-a"]["state"] == "idle"
    assert result["elapsed"] < 1.0
    # Waiter must be unregistered on exit so a long-lived bus doesn't
    # accumulate dead keys across many waits.
    assert "child-a" not in bus._waiters


def test_wait_with_bus_wakes_on_notify(populated_storage):
    """The core property of the refactor: a state-change ``notify``
    must wake the wait promptly — well under the legacy 0.5 s poll
    cadence AND the 2 s heartbeat cap.  Test fires a state update
    + notify after a short delay and asserts the wait returns quickly.
    """
    import threading as _t

    from turnstone.core.child_event_bus import ChildEventBus

    bus = ChildEventBus()
    client = _make_read_client_with_bus(populated_storage, bus)
    # child-b starts running; flip to idle + notify after the wait
    # blocks.  100 ms is enough that the wait is parked in event.wait()
    # but short enough that the test runs fast.
    timer = _t.Timer(
        0.1,
        lambda: (
            populated_storage.update_workstream_state("child-b", "idle"),
            bus.notify("child-b"),
        ),
    )
    timer.start()
    start = time.monotonic()
    result = client.wait_for_workstream(["child-b"], timeout=5.0, mode="any")
    elapsed = time.monotonic() - start
    assert result["complete"] is True
    assert result["results"]["child-b"]["state"] == "idle"
    # Bus-driven wake should fire well under 1 s; legacy poll would
    # take ~0.5 s but bus-driven should be ~0.1 s (the timer delay)
    # plus a few ms.  Generous 0.6 s budget for CI noise.
    assert elapsed < 0.6, f"wake-up too slow: {elapsed}s"


def test_wait_with_bus_unrelated_notify_does_not_wake(populated_storage):
    """A notify on a ws_id the wait isn't watching must NOT wake it —
    otherwise every state change anywhere on the system would shake
    every concurrent wait into a redundant storage snapshot.
    """
    from turnstone.core.child_event_bus import ChildEventBus

    bus = ChildEventBus()
    client = _make_read_client_with_bus(populated_storage, bus)
    # child-b is running indefinitely; mode='all' will time out unless
    # a relevant notify fires.  Fire only unrelated notifies — wait
    # should still hit the full timeout.
    import threading as _t

    def _fire_unrelated() -> None:
        for _ in range(5):
            bus.notify("ws-unrelated-1")
            bus.notify("ws-unrelated-2")
            time.sleep(0.05)

    t = _t.Thread(target=_fire_unrelated, daemon=True)
    t.start()
    start = time.monotonic()
    result = client.wait_for_workstream(["child-b"], timeout=0.5, mode="all")
    elapsed = time.monotonic() - start
    assert result["complete"] is False, "unrelated notify falsely satisfied wait"
    # Wait should burn its full timeout (give or take heartbeat
    # granularity).  The bus path doesn't have a 0.5 s poll, so the
    # bound is "approximately timeout".
    assert elapsed >= 0.5
    t.join(timeout=1.0)


def test_wait_with_bus_heartbeat_still_progresses_without_notify(populated_storage):
    """Without any notify, the wait must still progress through ticks
    via the heartbeat cap so ``progress_callback`` keeps firing for
    the sidebar UI.  Verified by counting callback firings over an
    interval longer than the heartbeat.
    """
    from turnstone.core.child_event_bus import ChildEventBus

    bus = ChildEventBus()
    client = _make_read_client_with_bus(populated_storage, bus)
    # Shrink the heartbeat for test speed via the ClassVar seam —
    # instance attribute shadows the class-level default.  Production
    # stays at 2.0 s; the test exercises the heartbeat-fires-without-
    # notify property in well under 1 s.
    client._WAIT_HEARTBEAT_INTERVAL = 0.1  # type: ignore[misc]
    snapshots: list[dict[str, dict[str, object]]] = []

    def _cb(snap: dict[str, dict[str, object]], _elapsed: float) -> None:
        snapshots.append(snap)

    # child-b is running indefinitely; wait will time out at 0.4 s.
    # With heartbeat = 0.1 s, we expect ~3-5 callback firings
    # (initial tick + ~3-4 heartbeats).  Loose lower bound to avoid
    # CI flakiness.
    start = time.monotonic()
    result = client.wait_for_workstream(["child-b"], timeout=0.4, mode="all", progress_callback=_cb)
    elapsed = time.monotonic() - start
    assert result["complete"] is False
    assert elapsed >= 0.4
    # At least 2 callback firings: the initial snapshot plus at least
    # one heartbeat-driven re-tick.  Tight upper bound would be
    # ~ceil(0.4/0.1) + 1 = 5 firings.
    assert len(snapshots) >= 2, f"heartbeat didn't fire: {len(snapshots)} snapshots"


def test_wait_with_bus_unregisters_waiter_on_exit(populated_storage):
    """Both the success path and the timeout path must unregister the
    waiter — otherwise a long-lived bus accumulates dead
    ``threading.Event`` instances forever.
    """
    from turnstone.core.child_event_bus import ChildEventBus

    bus = ChildEventBus()
    client = _make_read_client_with_bus(populated_storage, bus)
    # Success path (already-terminal child).
    client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    assert bus._waiters == {}, "success path leaked waiter"
    # Timeout path (running child, mode='all' that times out).
    client.wait_for_workstream(["child-a", "child-b"], timeout=0.3, mode="all")
    assert bus._waiters == {}, "timeout path leaked waiter"


def test_wait_with_bus_multi_waiter_independence(populated_storage):
    """Two concurrent waits on the same ws_id must be independent —
    one wait completing must not affect the other's wake-up state.
    Smoke-tests the multi-Event-per-bucket bus behaviour against the
    real wait-loop.
    """
    import threading as _t

    from turnstone.core.child_event_bus import ChildEventBus

    bus = ChildEventBus()
    client = _make_read_client_with_bus(populated_storage, bus)

    results: dict[str, dict[str, object]] = {}

    def _do_wait(label: str) -> None:
        results[label] = client.wait_for_workstream(["child-a"], timeout=5, mode="any")

    threads = [_t.Thread(target=_do_wait, args=(f"t{i}",), daemon=True) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    for label in ("t0", "t1", "t2"):
        assert results[label]["complete"] is True
        assert results[label]["results"]["child-a"]["state"] == "idle"
    # All waiters must be unregistered after exit.
    assert bus._waiters == {}


# ---------------------------------------------------------------------------
# wait_for_workstream — last-message bundling
# ---------------------------------------------------------------------------
#
# Each terminal child's last assistant turn (or a status sentinel) is
# bundled inline so the coord LLM doesn't need a follow-up
# inspect_workstream round-trip per ws.  The fields are additive
# (``message`` / ``truncated``), so existing wait tests stay green.


def test_wait_for_workstream_idle_returns_last_assistant_message(populated_storage):
    """A child that finished normally surfaces its final assistant
    turn inline so the coord doesn't have to inspect to read it."""
    populated_storage.save_message("child-a", "user", "what's the answer?")
    populated_storage.save_message("child-a", "assistant", "the answer is 42")
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    snap = result["results"]["child-a"]
    assert snap["state"] == "idle"
    assert snap["message"] == "the answer is 42"
    assert snap["truncated"] is False


def test_wait_for_workstream_idle_walks_past_trailing_tool_messages(populated_storage):
    """The most recent assistant turn often sits behind a few tool
    messages (assistant emits tool_calls → tool results land → final
    assistant content follows).  The walk must skip non-assistant
    rows when picking the last assistant content."""
    populated_storage.save_message("child-a", "user", "do the thing")
    populated_storage.save_message("child-a", "assistant", "calling tool")
    populated_storage.save_message("child-a", "tool", "tool output", tool_call_id="t1")
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    # The assistant message above is the most recent assistant turn —
    # the trailing tool row must not block extraction.
    assert result["results"]["child-a"]["message"] == "calling tool"


def test_wait_for_workstream_idle_skips_empty_assistant_with_tool_calls(populated_storage):
    """An assistant message with empty content + only tool_calls isn't
    a final answer — walk further back for the last assistant message
    that actually has text."""
    populated_storage.save_message("child-a", "user", "first turn")
    populated_storage.save_message("child-a", "assistant", "first assistant reply")
    populated_storage.save_message("child-a", "user", "second turn")
    populated_storage.save_message(
        "child-a", "assistant", "", tool_calls='[{"id": "t1", "name": "x"}]'
    )
    populated_storage.save_message("child-a", "tool", "tool result", tool_call_id="t1")
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    # Last assistant with non-empty content is the FIRST assistant message
    # — the empty-content tool-calls assistant must be skipped.
    assert result["results"]["child-a"]["message"] == "first assistant reply"


def test_wait_for_workstream_idle_no_assistant_returns_sentinel(populated_storage):
    """A workstream that reaches idle without an assistant turn in the
    tail (rare but possible for a freshly registered ws closed before
    generation, or a long-running ws whose final assistant message is
    buried beyond the tail window) gets a hedged sentinel rather than
    null — the model can distinguish 'no recent output' from 'still
    running'."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    snap = result["results"]["child-a"]
    assert snap["state"] == "idle"
    # No messages were saved for child-a in this test — sentinel kicks in.
    # Wording is hedged ("recent") because the tail-only walk can't
    # actually prove no assistant output exists in the full history.
    assert snap["message"] == "(no recent assistant output)"
    assert snap["truncated"] is False


def test_wait_for_workstream_error_returns_last_assistant_message(populated_storage):
    """An errored child still gets its last assistant turn surfaced —
    that's usually the most useful diagnostic ('I was about to ...
    when the error happened')."""
    populated_storage.update_workstream_state("child-a", "error")
    populated_storage.save_message("child-a", "user", "hi")
    populated_storage.save_message("child-a", "assistant", "partial output before crash")
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    snap = result["results"]["child-a"]
    assert snap["state"] == "error"
    assert snap["message"] == "partial output before crash"


def test_wait_for_workstream_error_with_no_output_returns_sentinel(populated_storage):
    """When error fires with no assistant content in the tail (e.g. a
    pre-flight provider auth failure that crashes before the model
    speaks, or a >18-parallel-tool-call burst whose only assistant
    row carries empty content), the same hedged sentinel applies.
    The wording deliberately doesn't claim 'before producing output'
    — the tail-only walk can't prove that.
    """
    populated_storage.update_workstream_state("child-a", "error")
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    snap = result["results"]["child-a"]
    assert snap["state"] == "error"
    assert snap["message"] == "(no recent assistant output)"
    assert snap["truncated"] is False


def test_wait_for_workstream_error_prefers_persisted_last_error(populated_storage):
    """When the worker thread persists ``last_error`` on a crash (e.g.
    provider 429 after retry exhaustion, model misconfig), the error
    text wins over the assistant tail — the actual cause is more
    actionable than a half-finished prior turn."""
    populated_storage.update_workstream_state("child-a", "error")
    populated_storage.save_message("child-a", "assistant", "partial output before crash")
    populated_storage.save_workstream_config(
        "child-a",
        {"last_error": "RateLimitError: 429 too many requests after 5 retries"},
    )
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    snap = result["results"]["child-a"]
    assert snap["state"] == "error"
    assert snap["message"] == "RateLimitError: 429 too many requests after 5 retries"
    assert snap["truncated"] is False


def test_wait_for_workstream_error_falls_back_to_assistant_when_no_last_error(populated_storage):
    """Legacy / pre-fix error rows (state=error, no last_error config)
    keep the existing assistant-tail behaviour — the upgrade is
    additive."""
    populated_storage.update_workstream_state("child-a", "error")
    populated_storage.save_message("child-a", "user", "hi")
    populated_storage.save_message("child-a", "assistant", "partial output before crash")
    # Note: no save_workstream_config call.
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    snap = result["results"]["child-a"]
    assert snap["message"] == "partial output before crash"


def test_wait_for_workstream_closed_returns_sentinel(populated_storage):
    """Closed children get a status sentinel rather than a partial
    last message — a half-finished thought from a workstream the
    operator explicitly closed isn't useful (and could be misleading)."""
    populated_storage.update_workstream_state("child-a", "closed")
    populated_storage.save_message("child-a", "assistant", "mid-thought when closed")
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    snap = result["results"]["child-a"]
    assert snap["state"] == "closed"
    assert snap["message"] == "(workstream closed)"
    assert snap["truncated"] is False


def test_wait_for_workstream_denied_returns_sentinel(populated_storage):
    """Cross-tenant / nonexistent ws_ids surface as denied — the
    sentinel lets the coord LLM recognise the rejection without
    parsing state strings on its own."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["unrelated"], timeout=5, mode="any")
    snap = result["results"]["unrelated"]
    assert snap["state"] == "denied"
    assert snap["message"].startswith("(workstream denied")
    assert snap["truncated"] is False


def test_wait_for_workstream_running_child_message_is_null(populated_storage):
    """A still-running child after a timeout must report
    ``message=None`` — anything else would be a partial last message
    pretending to be a final answer.  The coord uses null to know
    'still working, inspect later'."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a", "child-b"], timeout=1.0, mode="all")
    # mode='all' on (idle, running) hits the timeout — child-b is still
    # running and must come back with message=None.
    assert result["complete"] is False
    assert result["results"]["child-b"]["state"] == "running"
    assert result["results"]["child-b"]["message"] is None
    assert result["results"]["child-b"]["truncated"] is False


def test_wait_for_workstream_truncates_oversize_message(populated_storage):
    """A message past WAIT_MESSAGE_MAX_BYTES is truncated from the
    END (preserve the lead) and ``truncated=True`` so the coord LLM
    knows to inspect for the rest if it needs the full text."""
    from turnstone.console.coordinator_client import WAIT_MESSAGE_MAX_BYTES

    big = "A" * (WAIT_MESSAGE_MAX_BYTES * 2)
    populated_storage.save_message("child-a", "user", "hi")
    populated_storage.save_message("child-a", "assistant", big)
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    snap = result["results"]["child-a"]
    # Truncated — exactly the cap in bytes (single-byte chars), with the
    # head preserved.
    assert snap["truncated"] is True
    assert len(snap["message"].encode("utf-8")) == WAIT_MESSAGE_MAX_BYTES
    assert snap["message"].startswith("AAAA")


def test_wait_for_workstream_storage_failure_leaves_message_null(populated_storage, monkeypatch):
    """A transient storage error during the message read must not
    fail the wait — the coord still gets state/tokens/updated, and
    the per-ws ``message`` collapses to None so the model can fall
    back to inspect."""
    populated_storage.update_workstream_state("child-a", "idle")

    def _broken_load(*_a, **_kw):
        raise RuntimeError("simulated storage outage")

    monkeypatch.setattr(populated_storage, "load_messages", _broken_load)
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], timeout=5, mode="any")
    snap = result["results"]["child-a"]
    assert snap["state"] == "idle"
    assert snap["message"] is None
    assert snap["truncated"] is False


def test_wait_for_workstream_does_not_pollute_progress_callback(populated_storage):
    """The wait_progress SSE event shape is documented as separate
    from the tool result — the per-tick snapshot dicts handed to the
    progress callback must NOT carry the new ``message`` /
    ``truncated`` fields, since enrichment happens after the loop
    exits."""
    populated_storage.save_message("child-a", "assistant", "ok")
    client = _make_read_client(populated_storage)
    captured: list[dict[str, dict[str, Any]]] = []

    def _cb(snap: dict[str, dict[str, Any]], _elapsed: float) -> None:
        # Deep-copy so a later mutation by enrichment can't fool the
        # assertion (we want the shape AT CALLBACK TIME, not at end).
        import copy

        captured.append(copy.deepcopy(snap))

    client.wait_for_workstream(["child-a"], timeout=5, mode="any", progress_callback=_cb)
    assert captured  # at least one tick fired
    for tick in captured:
        for per_ws in tick.values():
            assert "message" not in per_ws
            assert "truncated" not in per_ws


# ---------------------------------------------------------------------------
# wait_for_workstream — helper-function unit tests
# ---------------------------------------------------------------------------


def test_truncate_wait_message_below_cap_is_passthrough():
    from turnstone.console.coordinator_client import _truncate_wait_message

    text, trunc = _truncate_wait_message("hello", 100)
    assert text == "hello"
    assert trunc is False


def test_truncate_wait_message_exact_cap_is_passthrough():
    from turnstone.console.coordinator_client import _truncate_wait_message

    text, trunc = _truncate_wait_message("a" * 5, 5)
    assert text == "aaaaa"
    assert trunc is False


def test_truncate_wait_message_oversize_truncates_to_byte_cap():
    from turnstone.console.coordinator_client import _truncate_wait_message

    text, trunc = _truncate_wait_message("a" * 10, 5)
    assert text == "aaaaa"
    assert trunc is True


def test_truncate_wait_message_handles_utf8_boundary():
    """A multi-byte codepoint must never be split — back off to a valid
    UTF-8 boundary even if it lands a couple bytes under the cap."""
    from turnstone.console.coordinator_client import _truncate_wait_message

    # "café" is 5 bytes (c=1, a=1, f=1, é=2).  Cap at 4 bytes lands
    # mid-codepoint on the é; truncation must back off to 3 bytes.
    text, trunc = _truncate_wait_message("café", 4)
    assert trunc is True
    assert text == "caf"
    # And the result must be valid UTF-8 — re-encoding doesn't error.
    text.encode("utf-8")


def test_truncate_wait_message_zero_or_negative_cap_returns_empty():
    from turnstone.console.coordinator_client import _truncate_wait_message

    text, trunc = _truncate_wait_message("anything", 0)
    assert text == ""
    assert trunc is True


def test_last_assistant_text_returns_content_when_present(populated_storage):
    """Pins the third leg of the tri-state contract: a populated tail
    returns the actual assistant content string (not ``""``, not
    ``None``).  Integration tests cover this through enrichment, but a
    direct unit test makes the contract harder to break in a refactor."""
    from turnstone.console.coordinator_client import _last_assistant_text

    populated_storage.save_message("child-a", "user", "hello")
    populated_storage.save_message("child-a", "assistant", "hi back")
    assert _last_assistant_text(populated_storage, "child-a") == "hi back"


def test_last_assistant_text_returns_empty_when_no_messages(populated_storage):
    from turnstone.console.coordinator_client import _last_assistant_text

    # child-a has no messages saved.
    assert _last_assistant_text(populated_storage, "child-a") == ""


def test_last_assistant_text_returns_none_on_storage_failure(populated_storage, monkeypatch):
    from turnstone.console.coordinator_client import _last_assistant_text

    def _broken(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(populated_storage, "load_messages", _broken)
    assert _last_assistant_text(populated_storage, "child-a") is None


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


def _task_client(tmp_path) -> CoordinatorClient:
    st = SQLiteBackend(str(tmp_path / "tasks.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    return _make_read_client(st)


def test_tasks_get_empty_envelope_on_fresh_ws(tmp_path):
    client = _task_client(tmp_path)
    env = client.tasks_get("coord-1")
    assert env == {"version": 1, "tasks": []}


def test_tasks_add_then_get_roundtrip(tmp_path):
    client = _task_client(tmp_path)
    task = client.tasks_add("coord-1", title="spawn worker")
    assert task["title"] == "spawn worker"
    assert task["status"] == "pending"
    env = client.tasks_get("coord-1")
    assert len(env["tasks"]) == 1
    assert env["tasks"][0]["id"] == task["id"]


def test_tasks_add_rejects_empty_title(tmp_path):
    client = _task_client(tmp_path)
    result = client.tasks_add("coord-1", title="   ")
    assert "error" in result


def test_tasks_add_rejects_invalid_status(tmp_path):
    client = _task_client(tmp_path)
    result = client.tasks_add("coord-1", title="x", status="nonsense")
    assert "error" in result


def test_tasks_add_rejects_title_over_200(tmp_path):
    """Silent truncation is a data-integrity footgun: the model may
    rely on the title it sent, not the one stored.  Reject instead."""
    client = _task_client(tmp_path)
    long_title = "a" * 201
    result = client.tasks_add("coord-1", title=long_title)
    assert "error" in result
    assert "too long" in result["error"]
    # Exactly 200 chars is the boundary and still accepted.
    boundary = "a" * 200
    task = client.tasks_add("coord-1", title=boundary)
    assert "error" not in task
    assert len(task["title"]) == 200


def test_tasks_update_rejects_title_over_200(tmp_path):
    client = _task_client(tmp_path)
    added = client.tasks_add("coord-1", title="original")
    result = client.tasks_update("coord-1", task_id=added["id"], title="b" * 201)
    assert "error" in result
    assert "too long" in result["error"]
    # Original title untouched when update rejected.
    env = client.tasks_get("coord-1")
    assert env["tasks"][0]["title"] == "original"


def test_tasks_update_by_id(tmp_path):
    client = _task_client(tmp_path)
    added = client.tasks_add("coord-1", title="plan")
    updated = client.tasks_update(
        "coord-1", task_id=added["id"], status="done", child_ws_id="ws-child"
    )
    assert updated["status"] == "done"
    assert updated["child_ws_id"] == "ws-child"


def test_tasks_update_missing_id(tmp_path):
    client = _task_client(tmp_path)
    result = client.tasks_update("coord-1", task_id="nope", status="done")
    assert "error" in result


def test_tasks_remove(tmp_path):
    client = _task_client(tmp_path)
    added = client.tasks_add("coord-1", title="plan")
    first = client.tasks_remove("coord-1", task_id=added["id"])
    assert first.get("ok") is True
    assert first.get("task_id") == added["id"]
    # Second remove of the same id returns a distinguishable not-found
    # error (NOT a silent False that would mask a corrupt envelope).
    second = client.tasks_remove("coord-1", task_id=added["id"])
    assert "error" in second
    assert "not found" in second["error"]
    assert client.tasks_get("coord-1")["tasks"] == []


def test_tasks_reorder_requires_permutation(tmp_path):
    client = _task_client(tmp_path)
    a = client.tasks_add("coord-1", title="a")
    b = client.tasks_add("coord-1", title="b")
    # Partial set — must reject.
    bad = client.tasks_reorder("coord-1", task_ids=[a["id"]])
    assert "error" in bad
    # Wrong id — reject.
    wrong = client.tasks_reorder("coord-1", task_ids=[a["id"], "ghost"])
    assert "error" in wrong
    # Valid permutation — accept.
    ok = client.tasks_reorder("coord-1", task_ids=[b["id"], a["id"]])
    assert ok.get("ok") is True
    env = client.tasks_get("coord-1")
    assert [t["id"] for t in env["tasks"]] == [b["id"], a["id"]]


def test_tasks_cross_ws_scope_violation_is_noop(tmp_path):
    client = _task_client(tmp_path)
    # Client is bound to coord-1; anything else returns an empty envelope
    # or an error without touching storage.
    assert client.tasks_get("other-ws") == {"version": 1, "tasks": []}
    res_add = client.tasks_add("other-ws", title="sneak")
    assert "error" in res_add
    res_remove = client.tasks_remove("other-ws", task_id="x")
    assert "error" in res_remove
    assert "scope violation" in res_remove["error"]


def test_tasks_corrupt_json_returns_empty_envelope(tmp_path):
    """A hand-edited / corrupt config row must not crash the tool."""
    st = SQLiteBackend(str(tmp_path / "tasks.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    st.save_workstream_config("coord-1", {"tasks": "{not json"})
    client = _make_read_client(st)
    env = client.tasks_get("coord-1")
    assert env == {"version": 1, "tasks": []}


def test_tasks_mutations_refuse_corrupt_envelope(tmp_path):
    """When the envelope is corrupt on disk, mutators must error out
    (rather than silently overwrite — lost-data safety)."""
    st = SQLiteBackend(str(tmp_path / "tasks.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    st.save_workstream_config("coord-1", {"tasks": "{not json"})
    client = _make_read_client(st)
    add_result = client.tasks_add("coord-1", title="new")
    assert "error" in add_result
    assert "corrupt" in add_result["error"]
    # Also: the corrupt blob is preserved after the refused mutation.
    assert st.load_workstream_config("coord-1").get("tasks") == "{not json"
    update_result = client.tasks_update("coord-1", task_id="x", status="done")
    assert "error" in update_result
    reorder_result = client.tasks_reorder("coord-1", task_ids=[])
    assert "error" in reorder_result
    remove_result = client.tasks_remove("coord-1", task_id="x")
    assert "error" in remove_result
    assert "corrupt" in remove_result["error"]


def test_tasks_add_enforces_capacity_cap(tmp_path, monkeypatch):
    from turnstone.console import coordinator_client as cc_module

    monkeypatch.setattr(cc_module, "_TASKS_MAX", 3)
    client = _task_client(tmp_path)
    for i in range(3):
        client.tasks_add("coord-1", title=f"t{i}")
    overflow = client.tasks_add("coord-1", title="no-room")
    assert "error" in overflow
    assert "capacity" in overflow["error"]
    # After a remove, add succeeds again.
    env = client.tasks_get("coord-1")
    client.tasks_remove("coord-1", task_id=env["tasks"][0]["id"])
    added = client.tasks_add("coord-1", title="retry")
    assert "error" not in added


def test_tasks_save_preserves_other_workstream_config_keys(tmp_path):
    """_save_tasks writes only the 'tasks' key so other keys survive."""
    st = SQLiteBackend(str(tmp_path / "tasks.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    st.save_workstream_config("coord-1", {"reasoning_effort": "high"})
    client = _make_read_client(st)
    client.tasks_add("coord-1", title="plan")
    config = st.load_workstream_config("coord-1")
    assert config.get("reasoning_effort") == "high"
    assert config.get("tasks")  # tasks wrote its key too


def test_live_cache_lru_eviction_caps_memory(tmp_path):
    """_live_cache must evict the oldest entry when inserting past the
    cap — long-running coordinators that walk many children otherwise
    grow the cache monotonically."""
    st = SQLiteBackend(str(tmp_path / "cache.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    client = _make_read_client(st)
    # Use the internal store helper directly — the HTTP-driven path is
    # exercised elsewhere; here we just verify the eviction semantics.
    cap = client._LIVE_CACHE_MAX
    for i in range(cap + 10):
        client._store_live_cache(f"ws-{i:04x}", 0.0, None)
    assert len(client._live_cache) == cap
    # The oldest 10 entries should have been evicted.
    for i in range(10):
        assert f"ws-{i:04x}" not in client._live_cache
    # The newest entries survived.
    for i in range(cap, cap + 10):
        assert f"ws-{i:04x}" in client._live_cache


def test_live_cache_touch_on_hit_moves_to_end(tmp_path):
    """A cache hit must reset the entry's LRU position so it's not
    evicted just because it was old by insertion order."""
    st = SQLiteBackend(str(tmp_path / "cache.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    client = _make_read_client(st)
    cap = client._LIVE_CACHE_MAX
    for i in range(cap):
        client._store_live_cache(f"ws-{i:04x}", 0.0, None)
    # "Touch" the oldest entry by reading it — use an HTTP stub that
    # would normally 200 but we want the cache path to intercept.
    # Simulate by directly calling the touch pathway.
    with client._live_cache_lock:
        client._live_cache.move_to_end("ws-0000")
    # Now insert one more — the SECOND-oldest should be evicted, not
    # the touched ws-0000.
    client._store_live_cache("ws-new", 0.0, None)
    assert "ws-0000" in client._live_cache
    assert "ws-0001" not in client._live_cache


# ---------------------------------------------------------------------------
# wait_for_workstream — since= hint + progress_callback (#bug-5, #18, #perf-3)
# ---------------------------------------------------------------------------


def test_wait_since_missing_entry_does_not_force_early_exit(populated_storage):
    """Regression for #bug-5: a ``since`` dict that does NOT contain
    the polled ws_id must not short-circuit the wait with
    complete=True on tick one.  Only ws_ids present in since_map are
    considered for the diff-exit check — others fall through to the
    normal mode='any'/'all' conditions.

    Scenario: single running child (``child-b``) + a ``since`` dict
    keyed on a disjoint id (``unrelated``).  mode='all' forces a full
    wait so the run can't early-return on a real terminal — we expect
    the wait to time out with complete=False, not exit immediately
    with complete=True because the previous (broken) _diff_since
    treated ``prev is None`` as changed for every polled wid.
    """
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(
        ["child-b"],
        timeout=1.0,
        mode="all",
        since={"unrelated": {"state": "idle", "tokens": 0, "updated": "prior"}},
    )
    assert result["complete"] is False
    assert result["elapsed"] >= 1.0
    assert result["results"]["child-b"]["state"] == "running"


def test_wait_since_matching_snapshot_falls_through_to_mode(populated_storage):
    """A ``since`` entry that exactly matches the current snapshot
    (state + tokens + updated all unchanged) does not trigger the
    diff-exit — the wait falls through to the normal mode condition
    for that wid."""
    client = _make_read_client(populated_storage)
    # First, grab the current snapshot.
    first = client.wait_for_workstream(["child-a"], timeout=1.0, mode="any")
    assert first["complete"] is True
    snap = first["results"]
    # Re-issue with since=<current snapshot> — nothing changed, but
    # child-a is real-terminal ('idle') so mode='any' completes again.
    second = client.wait_for_workstream(
        ["child-a"],
        timeout=1.0,
        mode="any",
        since=snap,
    )
    assert second["complete"] is True
    # Elapsed should be sub-second: the mode='any' condition fired on
    # tick one, not a tick-one false-positive from _diff_since.
    assert second["elapsed"] < 1.0


def test_wait_since_malformed_input_drops_silently(populated_storage):
    """Hostile / malformed since hints (non-dict top-level, non-dict
    values) degrade to empty since_map rather than raising — the wait
    is advisory, not a gatekeeper."""
    client = _make_read_client(populated_storage)
    # Non-dict since — coerced to empty.
    result = client.wait_for_workstream(
        ["child-a"],
        timeout=1.0,
        mode="any",
        since=["not", "a", "dict"],  # type: ignore[arg-type]
    )
    assert "error" not in result
    assert result["complete"] is True

    # Dict with non-dict values — those entries silently drop.
    result = client.wait_for_workstream(
        ["child-a"],
        timeout=1.0,
        mode="any",
        since={"child-a": "not-a-dict"},  # type: ignore[dict-item]
    )
    assert "error" not in result
    assert result["complete"] is True


def test_wait_progress_callback_invoked_per_tick(populated_storage):
    """The progress_callback is invoked once per poll tick with the
    current snapshot + elapsed seconds.  Snapshots carry state/tokens/
    updated for each polled ws_id."""
    client = _make_read_client(populated_storage)
    ticks: list[tuple[dict, float]] = []

    def _cb(snap, elapsed):  # type: ignore[no-untyped-def]
        ticks.append((dict(snap), elapsed))

    result = client.wait_for_workstream(
        ["child-a"],
        timeout=1.0,
        mode="any",
        progress_callback=_cb,
    )
    assert result["complete"] is True
    assert len(ticks) >= 1
    first_snap, _ = ticks[0]
    assert "child-a" in first_snap
    assert first_snap["child-a"]["state"] == "idle"


def test_wait_progress_callback_errors_dont_break_loop(populated_storage):
    """A buggy progress_callback must not break the wait — exceptions
    are swallowed so a broken observer can't wedge the model's tool call."""
    client = _make_read_client(populated_storage)

    def _bad_cb(snap, elapsed):  # type: ignore[no-untyped-def]
        raise RuntimeError("observer exploded")

    result = client.wait_for_workstream(
        ["child-a"],
        timeout=1.0,
        mode="any",
        progress_callback=_bad_cb,
    )
    # Wait itself still returns normally.
    assert result["complete"] is True


# ---------------------------------------------------------------------------
# cleanup_dead_task_child_refs (#bug-6, #13)
# ---------------------------------------------------------------------------


def _save_tasks(storage: SQLiteBackend, ws_id: str, tasks: list[dict[str, Any]]) -> None:
    """Helper: persist a minimal task envelope for a coordinator."""
    storage.save_workstream_config(
        ws_id,
        {"tasks": json.dumps({"version": 1, "tasks": tasks}, separators=(",", ":"))},
    )


def test_cleanup_dead_task_child_refs_blanks_dead_links(populated_storage):
    """Tasks whose child_ws_id references a missing workstream get the
    link blanked; tasks with live links (or no link) are untouched."""
    client = _make_read_client(populated_storage)
    _save_tasks(
        populated_storage,
        "coord-1",
        [
            {"id": "t1", "title": "alive-linked", "status": "done", "child_ws_id": "child-a"},
            {"id": "t2", "title": "dead-linked", "status": "done", "child_ws_id": "ghost-xyz"},
            {"id": "t3", "title": "unlinked", "status": "pending", "child_ws_id": ""},
        ],
    )
    blanked = client.cleanup_dead_task_child_refs("coord-1")
    assert blanked == 1
    envelope = client.tasks_get("coord-1")
    tasks_by_id = {t["id"]: t for t in envelope["tasks"]}
    # Live link preserved.
    assert tasks_by_id["t1"]["child_ws_id"] == "child-a"
    # Dead link blanked.
    assert tasks_by_id["t2"]["child_ws_id"] == ""
    # Unlinked task untouched.
    assert tasks_by_id["t3"]["child_ws_id"] == ""


def test_cleanup_dead_task_child_refs_all_alive_is_noop(populated_storage):
    """When every child_ws_id resolves, the cleanup returns 0 and does
    not rewrite the envelope (we verify via a no-op save spy)."""
    client = _make_read_client(populated_storage)
    _save_tasks(
        populated_storage,
        "coord-1",
        [{"id": "t1", "title": "alive", "status": "done", "child_ws_id": "child-a"}],
    )
    saves: list[dict[str, str]] = []
    real_save = populated_storage.save_workstream_config

    def _spy_save(ws_id, cfg):  # type: ignore[no-untyped-def]
        saves.append(cfg)
        return real_save(ws_id, cfg)

    populated_storage.save_workstream_config = _spy_save  # type: ignore[method-assign]
    try:
        blanked = client.cleanup_dead_task_child_refs("coord-1")
    finally:
        populated_storage.save_workstream_config = real_save  # type: ignore[method-assign]
    assert blanked == 0
    assert saves == []


def test_cleanup_dead_task_child_refs_empty_envelope(populated_storage):
    """A coordinator with no tasks persisted returns 0 without
    raising — the cleanup runs on every close, including those that
    never used the tasks tool."""
    client = _make_read_client(populated_storage)
    blanked = client.cleanup_dead_task_child_refs("coord-1")
    assert blanked == 0


def test_cleanup_dead_task_child_refs_corrupt_envelope_skips(populated_storage):
    """A corrupt envelope (unparseable JSON in workstream_config.tasks)
    returns 0 rather than raising — the cleanup is best-effort and
    must not block the close flow."""
    populated_storage.save_workstream_config("coord-1", {"tasks": "{not json"})
    client = _make_read_client(populated_storage)
    assert client.cleanup_dead_task_child_refs("coord-1") == 0


def test_cleanup_dead_task_child_refs_uses_task_lock(populated_storage):
    """The cleanup must acquire the same per-ws _task_lock that
    tasks_add/update/remove/reorder hold, so a close racing an
    in-flight mutation can't lose writes (#bug-6).  Verified by
    swapping the cached lock for a stand-in that records acquisition."""
    client = _make_read_client(populated_storage)

    class _RecordingLock:
        """Mimics threading.Lock — counts __enter__ / __exit__ pairs."""

        def __init__(self) -> None:
            self.acquired = 0
            self.released = 0

        def __enter__(self) -> _RecordingLock:
            self.acquired += 1
            return self

        def __exit__(self, *exc: Any) -> None:
            self.released += 1

    recording = _RecordingLock()
    # Prime the cache under the cache-lock so the client's _task_lock()
    # lookup returns our stand-in instead of allocating a real Lock.
    with client._task_lock_cache_lock:
        client._task_lock_cache["coord-1"] = recording  # type: ignore[assignment]
    client.cleanup_dead_task_child_refs("coord-1")
    assert recording.acquired == 1
    assert recording.released == 1


def test_cleanup_dead_task_child_refs_storage_batch_failure_swallows(populated_storage):
    """If get_workstreams_batch raises, the cleanup returns 0 rather
    than propagating — close flow is resilient to storage hiccups."""
    client = _make_read_client(populated_storage)
    _save_tasks(
        populated_storage,
        "coord-1",
        [{"id": "t1", "title": "dead", "status": "done", "child_ws_id": "ghost"}],
    )

    def _boom(ws_ids):  # type: ignore[no-untyped-def]
        raise RuntimeError("storage down")

    populated_storage.get_workstreams_batch = _boom  # type: ignore[method-assign]
    assert client.cleanup_dead_task_child_refs("coord-1") == 0


# ---------------------------------------------------------------------------
# inspect_workstream — three-tier output compression
# ---------------------------------------------------------------------------
#
# A coord doing a fan-out wave against tool-heavy children would
# otherwise blow the context budget on raw output alone.  Mirrors the
# search tool's Tier-1/Tier-2/Tier-3 ladder.


def _make_inspect_result(
    *, ws_id: str = "ws-test", state: str = "running", n_messages: int = 5
) -> dict[str, Any]:
    """Build an inspect-result dict shaped like ``coordinator_client.inspect()``.

    Production output keys (``ws_id``, ``skill_id``) mirror the storage
    row that ``inspect()`` spreads from ``get_workstream``.  Tests that
    synthesize an inspect result must match these keys — otherwise a
    formatter that looks at the production keys silently emits null
    values against a fixture that uses different ones (real bug-1
    regression source: skeleton tier read ``skill`` from a fixture
    that wrote ``skill`` while production wrote ``skill_id``).
    """
    return {
        "ws_id": ws_id,
        "state": state,
        "title": "test workstream",
        "skill_id": "researcher",
        "messages": [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i} content"}
            for i in range(n_messages)
        ],
    }


def test_format_inspect_tiered_full_fits_returns_full_tier():
    """Small payloads pass through with `_tier='full'` — no compression."""
    from turnstone.console.coordinator_client import _format_inspect_tiered

    result = _make_inspect_result(n_messages=3)
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    assert parsed["_tier"] == "full"
    # Every message verbatim.
    assert len(parsed["messages"]) == 3
    assert parsed["messages"][0]["content"] == "msg 0 content"


def test_format_inspect_tiered_compact_when_full_exceeds_budget():
    """Large messages trigger the compact tier — head/tail-snipped
    content with the rest of the row intact."""
    from turnstone.console.coordinator_client import (
        _INSPECT_MSG_CONTENT_HEAD,
        _INSPECT_MSG_CONTENT_TAIL,
        _INSPECT_OUTPUT_BUDGET,
        _format_inspect_tiered,
    )

    # Each message ~5KB; with 20 messages, full tier blows the 32KB budget.
    fat = "X" * 5000
    result = {
        "id": "ws-fat",
        "state": "running",
        "messages": [{"role": "assistant", "content": fat} for _ in range(20)],
    }
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    assert parsed["_tier"] == "compact"
    # Every message preserved (compact keeps the count, just snips content).
    assert len(parsed["messages"]) == 20
    # Head/tail snip kicked in.
    msg_content = parsed["messages"][0]["content"]
    assert msg_content.startswith("X" * _INSPECT_MSG_CONTENT_HEAD)
    assert msg_content.endswith("X" * _INSPECT_MSG_CONTENT_TAIL)
    assert "chars elided" in msg_content
    # Budget invariant — the load-bearing contract of the formatter.
    # Without this assertion, a future change to ``_tier_note`` or
    # ``_compact_message`` could push the output over budget and the
    # ``_truncate_output`` head+tail safety net would silently mask
    # the regression, re-introducing the middle-message-drop pathology.
    assert len(out) <= _INSPECT_OUTPUT_BUDGET


def test_format_inspect_tiered_compact_when_content_below_snip_threshold():
    """When per-message content is below the snip threshold but the
    message COUNT alone overflows the budget, compact tier must still
    stay within budget — by trimming the message list (head + tail of
    messages) rather than degrading straight to skeleton.  Bug-3
    regression cover: with 400 × 100-char messages, the original
    formatter fell through to skeleton because adding ``_tier_note``
    to an un-snipped tier-2 produced output strictly larger than
    tier-1 (both over budget).  The fix preserves messages from both
    ends of the list and inserts an ``_omitted`` sentinel."""
    from turnstone.console.coordinator_client import (
        _INSPECT_OUTPUT_BUDGET,
        _format_inspect_tiered,
    )

    # 400 × ~100 chars → Tier-1 ~53 KB (over budget), per-message
    # content under the 964-char snip threshold so content-snipping
    # saves nothing.  Without the list-trim rung the formatter would
    # fall to skeleton and drop all 400 messages.
    smallish = "S" * 100
    result = {
        "ws_id": "ws-many-small",
        "state": "running",
        "messages": [
            {"role": "assistant" if i % 2 == 0 else "user", "content": smallish} for i in range(400)
        ],
    }
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    # Should NOT fall through to skeleton — message-list trim preserves
    # head + tail of the conversation.
    assert parsed["_tier"] == "compact"
    assert "messages" in parsed
    # Some messages must survive; the trim shape is head + tail with an
    # ``_omitted`` sentinel between them.
    assert len(parsed["messages"]) > 0
    assert len(parsed["messages"]) < 400
    # Budget invariant.
    assert len(out) <= _INSPECT_OUTPUT_BUDGET


def test_format_inspect_tiered_skeleton_when_compact_also_exceeds_budget():
    """Tier 3 fallback: counts + last assistant preview only.  Trigger by
    flooding with messages whose content is a multi-block list — the
    snipper correctly leaves non-string content unchanged (mirrors
    Anthropic/OpenAI multi-block content shape), so even after the
    (5, 10) message-list trim the surviving 15 messages don't fit in
    the 32 KB budget."""
    from turnstone.console.coordinator_client import (
        _INSPECT_OUTPUT_BUDGET,
        _format_inspect_tiered,
    )

    # 50 messages × multi-block content (~30 KB each — list-shape
    # content bypasses the head/tail string snipper because lists
    # aren't strings).  Even (5, 10) trim leaves 15 × 30 KB which
    # blows the 32 KB budget — forces skeleton.
    fat_block = {"type": "text", "text": "Y" * 3000}
    result = {
        "ws_id": "ws-flood",
        "state": "running",
        "title": "flood",
        "skill_id": "researcher",
        "messages": [
            {
                "role": "assistant" if i % 2 == 0 else "user",
                "content": [fat_block] * 10,
            }
            for i in range(50)
        ],
    }
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    assert parsed["_tier"] == "skeleton"
    assert parsed["message_count"] == 50
    # Role distribution surfaces — the "what shape of activity" signal.
    assert parsed["roles"]["assistant"] == 25
    assert parsed["roles"]["user"] == 25
    # No `messages` field at skeleton tier — only the aggregate signal.
    assert "messages" not in parsed
    # Budget invariant.
    assert len(out) <= _INSPECT_OUTPUT_BUDGET


def test_format_inspect_tiered_skeleton_keeps_terminal_state_fields():
    """``close_reason`` / ``last_error`` survive the skeleton fall — they're
    small, load-bearing, and the operator needs them to understand WHY
    a terminal child landed in its state."""
    from turnstone.console.coordinator_client import (
        _INSPECT_OUTPUT_BUDGET,
        _format_inspect_tiered,
    )

    # Same flood pattern as the bare-skeleton test (multi-block content
    # bypasses the string snipper) — paired with terminal-state fields
    # that must survive the skeleton fall.
    fat_block = {"type": "text", "text": "Z" * 3000}
    result = {
        "ws_id": "ws-closed",
        "state": "closed",
        "title": "done",
        "skill_id": "researcher",
        "messages": [{"role": "user", "content": [fat_block] * 10} for _ in range(50)],
        "close_reason": "task complete: report attached",
        "live": None,  # filtered by truthy check
    }
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    assert parsed["_tier"] == "skeleton"
    assert parsed["close_reason"] == "task complete: report attached"
    # Falsy ``live`` doesn't bleed through.
    assert "live" not in parsed
    assert len(out) <= _INSPECT_OUTPUT_BUDGET


def test_format_inspect_tiered_error_shapes_bypass_tiering():
    """Cross-tenant / not-found responses keep their original shape — they
    carry no messages, are already tiny, and changing them would break
    callers that key on the ``error`` field."""
    from turnstone.console.coordinator_client import _format_inspect_tiered

    result = {"error": "workstream not found", "ws_id": "ws-foreign"}
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    assert parsed == {"error": "workstream not found", "ws_id": "ws-foreign"}
    # No `_tier` annotation — error shapes are self-describing.
    assert "_tier" not in parsed


def test_format_inspect_tiered_compact_preserves_tool_call_linkage():
    """Compact tier keeps ``tool_name`` / ``tool_call_id`` / ``name`` so a
    model reading the snipped trace can still pair a tool call to its
    response — the linkage is load-bearing for "what happened" signal."""
    from turnstone.console.coordinator_client import _format_inspect_tiered

    fat = "Q" * 5000
    result = {
        "ws_id": "ws-tools",
        "state": "running",
        "messages": [
            {
                "role": "assistant",
                "content": fat,
                "tool_name": "bash",
                "tool_call_id": "call-1",
            }
            for _ in range(20)
        ],
    }
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    assert parsed["_tier"] == "compact"
    first = parsed["messages"][0]
    assert first["tool_name"] == "bash"
    assert first["tool_call_id"] == "call-1"


def test_format_inspect_tiered_compact_preserves_assistant_tool_calls():
    """Compact tier must preserve the assistant-side ``tool_calls`` list
    (OpenAI shape: ``[{id, type, function: {name, arguments}}]``) so a
    model reading the snipped trace can see WHICH tool was called and
    pair it with the corresponding result row via ``id`` ↔ ``tool_call_id``.
    Bug-2 regression cover: the pre-fix compactor stripped ``tool_calls``,
    leaving the audit reader with a tool-result orphan against an
    invisible call.

    ``function.arguments`` strings are snipped head/tail (analogous to
    content) because they can be multi-KB JSON; ``id`` and
    ``function.name`` are preserved verbatim — they're the linkage."""
    from turnstone.console.coordinator_client import (
        _INSPECT_TOOL_ARG_HEAD,
        _INSPECT_TOOL_ARG_TAIL,
        _format_inspect_tiered,
    )

    fat_content = "C" * 5000  # forces compact tier
    fat_args = "A" * 5000  # forces argument snipping
    tool_calls = [
        {
            "id": "call-abc-123",
            "type": "function",
            "function": {"name": "bash", "arguments": fat_args},
        },
        {
            "id": "call-def-456",
            "type": "function",
            "function": {"name": "read_file", "arguments": fat_args},
        },
    ]
    result = {
        "ws_id": "ws-tool-calls",
        "state": "running",
        "messages": [
            {"role": "assistant", "content": fat_content, "tool_calls": tool_calls}
            for _ in range(20)
        ],
    }
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    assert parsed["_tier"] == "compact"
    first = parsed["messages"][0]
    # tool_calls survives compaction.
    assert "tool_calls" in first
    assert len(first["tool_calls"]) == 2
    # Linkage fields verbatim.
    assert first["tool_calls"][0]["id"] == "call-abc-123"
    assert first["tool_calls"][0]["function"]["name"] == "bash"
    assert first["tool_calls"][1]["id"] == "call-def-456"
    assert first["tool_calls"][1]["function"]["name"] == "read_file"
    # arguments snipped head/tail — both prefix and suffix preserved.
    snipped_args = first["tool_calls"][0]["function"]["arguments"]
    assert snipped_args.startswith("A" * _INSPECT_TOOL_ARG_HEAD)
    assert snipped_args.endswith("A" * _INSPECT_TOOL_ARG_TAIL)
    assert "chars elided" in snipped_args


def test_format_inspect_tiered_compact_passes_small_messages_through_unsnipped():
    """Messages under the snip threshold pass through verbatim at compact
    tier — snipping a 100-byte message costs more bytes (the elision
    marker) than it saves."""
    from turnstone.console.coordinator_client import _format_inspect_tiered

    # Mix: a few large messages force compact tier; small messages must
    # not be snipped.
    big = "B" * 5000
    small = "S" * 50
    result = {
        "id": "ws-mixed",
        "state": "running",
        "messages": [{"role": "assistant", "content": big} for _ in range(15)]
        + [{"role": "user", "content": small}],
    }
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    assert parsed["_tier"] == "compact"
    # The trailing small message is exact, not snipped.
    assert parsed["messages"][-1]["content"] == small


def test_format_inspect_tiered_emits_tier_note_when_compressed():
    """The ``_tier_note`` advisory tells the LLM how to ask for a tighter
    or fuller view next time — actionable feedback rather than a bare
    "we compressed your output" signal."""
    from turnstone.console.coordinator_client import _format_inspect_tiered

    fat = "F" * 5000
    result = {
        "id": "ws-noted",
        "state": "running",
        "messages": [{"role": "assistant", "content": fat} for _ in range(20)],
    }
    out = _format_inspect_tiered(result)
    parsed = json.loads(out)
    assert "_tier_note" in parsed
    assert "message_limit" in parsed["_tier_note"]


def test_format_inspect_tiered_full_tier_omits_tier_note():
    """When the full tier fits, no note is emitted — the absence of a
    note is the signal that nothing was compressed."""
    from turnstone.console.coordinator_client import _format_inspect_tiered

    out = _format_inspect_tiered(_make_inspect_result(n_messages=2))
    parsed = json.loads(out)
    assert parsed["_tier"] == "full"
    assert "_tier_note" not in parsed
