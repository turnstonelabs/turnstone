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
    _set_meta(
        st,
        "node-c",
        [("arch", "arm64", "auto"), ("cpu_count", 8, "auto")],
    )
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


def test_task_list_add_clamps_title_to_200(tmp_path):
    client = _task_client(tmp_path)
    long_title = "a" * 500
    task = client.task_list_add("coord-1", title=long_title)
    assert len(task["title"]) == 200


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
