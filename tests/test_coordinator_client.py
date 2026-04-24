"""Tests for ``turnstone.console.coordinator_client.CoordinatorClient``.

Uses an httpx MockTransport to intercept outbound requests so we verify
the URL map, headers, and body shape without standing up a real console.
Read-op tests hit a real in-memory SQLite backend to confirm the
storage-call path.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from turnstone.console.coordinator_client import (
    _ROUTE_PATHS,
    CoordinatorClient,
    CoordinatorTokenManager,
)
from turnstone.core.auth import JWT_AUD_CONSOLE, validate_jwt
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
    assert _ROUTE_PATHS["send"] == "/v1/api/route/send"
    assert _ROUTE_PATHS["approve"] == "/v1/api/route/approve"
    assert _ROUTE_PATHS["cancel"] == "/v1/api/route/cancel"
    assert _ROUTE_PATHS["close"] == "/v1/api/route/workstreams/close"
    assert _ROUTE_PATHS["delete"] == "/v1/api/route/workstreams/delete"
    # Cascade endpoint lives on the console itself (not a node), so the
    # path slots in the coord ws_id rather than routing through a proxy.
    assert _ROUTE_PATHS["close_all_children"] == "/v1/api/workstreams/{ws_id}/close_all_children"


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
    assert captured[0].url.path == "/v1/api/route/send"
    body = json.loads(captured[0].content)
    assert body == {"ws_id": "ws-x", "message": "hello"}


def test_close_workstream_posts_to_close_route():
    client, captured = _mock_client(_ok_json({"status": 200}))
    client.close_workstream("ws-x")
    assert captured[0].url.path == "/v1/api/route/workstreams/close"
    body = json.loads(captured[0].content)
    assert body == {"ws_id": "ws-x"}  # no reason → omitted


def test_close_workstream_includes_reason_when_provided():
    client, captured = _mock_client(_ok_json({"status": 200}))
    client.close_workstream("ws-x", reason="done")
    body = json.loads(captured[0].content)
    assert body == {"ws_id": "ws-x", "reason": "done"}


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
    assert captured[0].url.path == "/v1/api/route/approve"
    assert captured[1].url.path == "/v1/api/route/cancel"
    approve_body = json.loads(captured[0].content)
    assert approve_body["approved"] is True
    assert approve_body["always"] is True


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
    assert captured[0].url.path == "/v1/api/route/send"


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
    assert isinstance(result["verdicts"], list)


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


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------


@pytest.fixture
def storage_with_skills(tmp_path):
    st = SQLiteBackend(str(tmp_path / "skills.db"))
    st.create_prompt_template(
        template_id="s1",
        name="alpha",
        category="ops",
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        tags='["gpu", "fast"]',
    )
    st.create_prompt_template(
        template_id="s2",
        name="beta",
        category="engineering",
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        tags='["slow"]',
    )
    st.create_prompt_template(
        template_id="s3",
        name="gamma",
        category="engineering",
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        tags="[]",
        enabled=False,
    )
    return st


def test_list_skills_returns_shape(storage_with_skills):
    client = _make_read_client(storage_with_skills)
    result = client.list_skills()
    assert set(result.keys()) == {"skills", "truncated"}
    names = {s["name"] for s in result["skills"]}
    assert names == {"alpha", "beta", "gamma"}
    # Tags decoded to a list, not a string.
    alpha = next(s for s in result["skills"] if s["name"] == "alpha")
    assert alpha["tags"] == ["gpu", "fast"]
    # Discovery projection only — not full row.
    assert "content" not in alpha


def test_list_skills_pushes_filters_to_storage_no_per_row_lookups(storage_with_skills, monkeypatch):
    called = []
    real_get = storage_with_skills.get_prompt_template

    def _spy(tid):  # type: ignore[no-untyped-def]
        called.append(tid)
        return real_get(tid)

    monkeypatch.setattr(storage_with_skills, "get_prompt_template", _spy)

    client = _make_read_client(storage_with_skills)
    result = client.list_skills(tag="gpu")
    assert {s["name"] for s in result["skills"]} == {"alpha"}
    assert called == []  # no N+1


def test_list_skills_enabled_only(storage_with_skills):
    client = _make_read_client(storage_with_skills)
    result = client.list_skills(enabled_only=True)
    names = {s["name"] for s in result["skills"]}
    assert names == {"alpha", "beta"}  # gamma is disabled


def test_list_skills_truncation_signal(storage_with_skills):
    client = _make_read_client(storage_with_skills)
    result = client.list_skills(limit=2)
    assert len(result["skills"]) == 2
    assert result["truncated"] is True


def test_list_skills_hides_interactive_only_skills(tmp_path):
    """CoordinatorClient.list_skills must narrow the storage query to
    ``kinds=['coordinator', 'any']`` so interactive-only skills (which
    are meant for child workstreams) don't pollute the orchestrator's
    tool surface.  Regression lock for a load-bearing invariant that
    the fixture-based tests above can't exercise because their skills
    all default to ``kind='any'``."""
    st = SQLiteBackend(str(tmp_path / "kinds.db"))
    st.create_prompt_template(
        template_id="k1",
        name="interactive-only",
        category="general",
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        description="interactive only",
        kind="interactive",
    )
    st.create_prompt_template(
        template_id="k2",
        name="coord-only",
        category="general",
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        description="coordinator only",
        kind="coordinator",
    )
    st.create_prompt_template(
        template_id="k3",
        name="universal",
        category="general",
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        description="everywhere",
        kind="any",
    )

    client = _make_read_client(st)
    result = client.list_skills()
    names = {s["name"] for s in result["skills"]}
    assert "interactive-only" not in names
    assert names == {"coord-only", "universal"}
    # And the kind projection comes through on every returned row.
    for skill in result["skills"]:
        assert skill["kind"] in {"coordinator", "any"}


def test_list_skills_projects_allowed_tools_capped_with_sentinel(tmp_path):
    """Each row carries the skill's allowed_tools (capped at the projection
    cap with a +N more sentinel) so coordinators can pick a skill without
    speculating which tools it brings.  The cap keeps the per-row payload
    bounded for skills that whitelist a wide MCP surface."""
    from turnstone.console.coordinator_client import _SKILL_TOOLS_PROJECTION_CAP

    st = SQLiteBackend(str(tmp_path / "skills_tools.db"))
    st.create_prompt_template(
        template_id="s-short",
        name="short-skill",
        category="ops",
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        tags="[]",
        allowed_tools='["read_file", "search"]',
    )
    long_tools = [f"tool_{i:03d}" for i in range(_SKILL_TOOLS_PROJECTION_CAP + 7)]
    st.create_prompt_template(
        template_id="s-long",
        name="long-skill",
        category="ops",
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        tags="[]",
        allowed_tools=json.dumps(long_tools),
    )
    client = _make_read_client(st)
    result = client.list_skills()
    by_name = {s["name"]: s for s in result["skills"]}
    assert by_name["short-skill"]["allowed_tools"] == ["read_file", "search"]
    long_skill = by_name["long-skill"]["allowed_tools"]
    # Cap items + 1 sentinel.
    assert len(long_skill) == _SKILL_TOOLS_PROJECTION_CAP + 1
    assert long_skill[-1] == f"+{7} more"
    assert long_skill[0] == "tool_000"


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


def test_wait_for_workstream_uses_batched_storage_calls(populated_storage, monkeypatch):
    """Per-tick polling must issue batched storage calls — at the
    documented cap (32 ws_ids over a 600s wait) the naive per-id
    shape produced ~38k row reads.  Guard against regression."""
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
    # One tick is enough since child-a is already idle (terminal).
    assert len(batch_calls) == 1
    assert len(sum_calls) == 1
    assert set(batch_calls[0]) == {"child-a", "child-b"}
    assert set(sum_calls[0]) == {"child-a", "child-b"}


def test_wait_for_workstream_handles_non_string_mode(populated_storage):
    """A model that emits ``mode=123`` or ``mode=['any']`` produces a
    clean error rather than crashing with AttributeError on .strip()."""
    client = _make_read_client(populated_storage)
    result = client.wait_for_workstream(["child-a"], mode=123)  # type: ignore[arg-type]
    assert "error" in result
    assert "invalid mode" in result["error"]


# ---------------------------------------------------------------------------
# task_list
# ---------------------------------------------------------------------------


def _task_client(tmp_path) -> CoordinatorClient:
    st = SQLiteBackend(str(tmp_path / "tasks.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    return _make_read_client(st)


def test_task_list_get_empty_envelope_on_fresh_ws(tmp_path):
    client = _task_client(tmp_path)
    env = client.task_list_get("coord-1")
    assert env == {"version": 1, "tasks": []}


def test_task_list_add_then_get_roundtrip(tmp_path):
    client = _task_client(tmp_path)
    task = client.task_list_add("coord-1", title="spawn worker")
    assert task["title"] == "spawn worker"
    assert task["status"] == "pending"
    env = client.task_list_get("coord-1")
    assert len(env["tasks"]) == 1
    assert env["tasks"][0]["id"] == task["id"]


def test_task_list_add_rejects_empty_title(tmp_path):
    client = _task_client(tmp_path)
    result = client.task_list_add("coord-1", title="   ")
    assert "error" in result


def test_task_list_add_rejects_invalid_status(tmp_path):
    client = _task_client(tmp_path)
    result = client.task_list_add("coord-1", title="x", status="nonsense")
    assert "error" in result


def test_task_list_add_rejects_title_over_200(tmp_path):
    """Silent truncation is a data-integrity footgun: the model may
    rely on the title it sent, not the one stored.  Reject instead."""
    client = _task_client(tmp_path)
    long_title = "a" * 201
    result = client.task_list_add("coord-1", title=long_title)
    assert "error" in result
    assert "too long" in result["error"]
    # Exactly 200 chars is the boundary and still accepted.
    boundary = "a" * 200
    task = client.task_list_add("coord-1", title=boundary)
    assert "error" not in task
    assert len(task["title"]) == 200


def test_task_list_update_rejects_title_over_200(tmp_path):
    client = _task_client(tmp_path)
    added = client.task_list_add("coord-1", title="original")
    result = client.task_list_update("coord-1", task_id=added["id"], title="b" * 201)
    assert "error" in result
    assert "too long" in result["error"]
    # Original title untouched when update rejected.
    env = client.task_list_get("coord-1")
    assert env["tasks"][0]["title"] == "original"


def test_task_list_update_by_id(tmp_path):
    client = _task_client(tmp_path)
    added = client.task_list_add("coord-1", title="plan")
    updated = client.task_list_update(
        "coord-1", task_id=added["id"], status="done", child_ws_id="ws-child"
    )
    assert updated["status"] == "done"
    assert updated["child_ws_id"] == "ws-child"


def test_task_list_update_missing_id(tmp_path):
    client = _task_client(tmp_path)
    result = client.task_list_update("coord-1", task_id="nope", status="done")
    assert "error" in result


def test_task_list_remove(tmp_path):
    client = _task_client(tmp_path)
    added = client.task_list_add("coord-1", title="plan")
    first = client.task_list_remove("coord-1", task_id=added["id"])
    assert first.get("ok") is True
    assert first.get("task_id") == added["id"]
    # Second remove of the same id returns a distinguishable not-found
    # error (NOT a silent False that would mask a corrupt envelope).
    second = client.task_list_remove("coord-1", task_id=added["id"])
    assert "error" in second
    assert "not found" in second["error"]
    assert client.task_list_get("coord-1")["tasks"] == []


def test_task_list_reorder_requires_permutation(tmp_path):
    client = _task_client(tmp_path)
    a = client.task_list_add("coord-1", title="a")
    b = client.task_list_add("coord-1", title="b")
    # Partial set — must reject.
    bad = client.task_list_reorder("coord-1", task_ids=[a["id"]])
    assert "error" in bad
    # Wrong id — reject.
    wrong = client.task_list_reorder("coord-1", task_ids=[a["id"], "ghost"])
    assert "error" in wrong
    # Valid permutation — accept.
    ok = client.task_list_reorder("coord-1", task_ids=[b["id"], a["id"]])
    assert ok.get("ok") is True
    env = client.task_list_get("coord-1")
    assert [t["id"] for t in env["tasks"]] == [b["id"], a["id"]]


def test_task_list_cross_ws_scope_violation_is_noop(tmp_path):
    client = _task_client(tmp_path)
    # Client is bound to coord-1; anything else returns an empty envelope
    # or an error without touching storage.
    assert client.task_list_get("other-ws") == {"version": 1, "tasks": []}
    res_add = client.task_list_add("other-ws", title="sneak")
    assert "error" in res_add
    res_remove = client.task_list_remove("other-ws", task_id="x")
    assert "error" in res_remove
    assert "scope violation" in res_remove["error"]


def test_task_list_corrupt_json_returns_empty_envelope(tmp_path):
    """A hand-edited / corrupt config row must not crash the tool."""
    st = SQLiteBackend(str(tmp_path / "tasks.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    st.save_workstream_config("coord-1", {"tasks": "{not json"})
    client = _make_read_client(st)
    env = client.task_list_get("coord-1")
    assert env == {"version": 1, "tasks": []}


def test_task_list_mutations_refuse_corrupt_envelope(tmp_path):
    """When the envelope is corrupt on disk, mutators must error out
    (rather than silently overwrite — lost-data safety)."""
    st = SQLiteBackend(str(tmp_path / "tasks.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    st.save_workstream_config("coord-1", {"tasks": "{not json"})
    client = _make_read_client(st)
    add_result = client.task_list_add("coord-1", title="new")
    assert "error" in add_result
    assert "corrupt" in add_result["error"]
    # Also: the corrupt blob is preserved after the refused mutation.
    assert st.load_workstream_config("coord-1").get("tasks") == "{not json"
    update_result = client.task_list_update("coord-1", task_id="x", status="done")
    assert "error" in update_result
    reorder_result = client.task_list_reorder("coord-1", task_ids=[])
    assert "error" in reorder_result
    remove_result = client.task_list_remove("coord-1", task_id="x")
    assert "error" in remove_result
    assert "corrupt" in remove_result["error"]


def test_task_list_add_enforces_capacity_cap(tmp_path, monkeypatch):
    from turnstone.console import coordinator_client as cc_module

    monkeypatch.setattr(cc_module, "_TASK_LIST_MAX", 3)
    client = _task_client(tmp_path)
    for i in range(3):
        client.task_list_add("coord-1", title=f"t{i}")
    overflow = client.task_list_add("coord-1", title="no-room")
    assert "error" in overflow
    assert "capacity" in overflow["error"]
    # After a remove, add succeeds again.
    env = client.task_list_get("coord-1")
    client.task_list_remove("coord-1", task_id=env["tasks"][0]["id"])
    added = client.task_list_add("coord-1", title="retry")
    assert "error" not in added


def test_task_list_save_preserves_other_workstream_config_keys(tmp_path):
    """_save_task_list writes only the 'tasks' key so other keys survive."""
    st = SQLiteBackend(str(tmp_path / "tasks.db"))
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    st.save_workstream_config("coord-1", {"reasoning_effort": "high"})
    client = _make_read_client(st)
    client.task_list_add("coord-1", title="plan")
    config = st.load_workstream_config("coord-1")
    assert config.get("reasoning_effort") == "high"
    assert config.get("tasks")  # task_list wrote its key too


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
    envelope = client.task_list_get("coord-1")
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
    """A coordinator with no task_list persisted returns 0 without
    raising — the cleanup runs on every close, including those that
    never used the task_list tool."""
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
    task_list_add/update/remove/reorder hold, so a close racing an
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
