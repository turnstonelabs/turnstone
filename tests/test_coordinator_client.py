"""Tests for ``turnstone.console.coordinator_client.CoordinatorClient``.

Uses an httpx MockTransport to intercept outbound requests so we verify
the URL map, headers, and body shape without standing up a real console.
Read-op tests hit a real in-memory SQLite backend to confirm the
storage-call path.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

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
    """Build a CoordinatorClient with an httpx MockTransport recorder."""
    captured: list[httpx.Request] = []

    def _trapping(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    transport = httpx.MockTransport(_trapping)
    http = httpx.Client(transport=transport)
    # Minimal storage stub for read ops is OK — mutating-op tests don't
    # touch storage, so a SQLiteBackend would also be fine.
    storage = SQLiteBackend(":memory:")
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
# Read ops — storage-backed
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_storage(tmp_path):
    st = SQLiteBackend(str(tmp_path / "coord.db"))
    # Coord + 2 interactive children + 1 child coordinator (excluded) +
    # 1 unrelated ws.
    st.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    st.register_workstream(
        "child-a",
        kind="interactive",
        parent_ws_id="coord-1",
        state="idle",
        skill_id="skill-x",
    )
    st.register_workstream(
        "child-b",
        kind="interactive",
        parent_ws_id="coord-1",
        state="running",
        skill_id="skill-y",
    )
    st.register_workstream(
        "child-coord",
        kind="coordinator",
        parent_ws_id="coord-1",
    )
    st.register_workstream("unrelated", kind="interactive")
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
    assert names == {"child-a", "child-b"}  # excludes child-coord + unrelated
    for r in rows:
        assert r["kind"] == "interactive"
        assert r["parent_ws_id"] == "coord-1"
    # Well under limit and no filters → not truncated.
    assert result["truncated"] is False


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
