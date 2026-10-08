"""Body-level tests for the saved-workstreams handler factories.

Covers the unified (multi-kind) saved-list handler the console mounts for its
L-shell dashboard and the single-kind :func:`make_saved_handler`: kind
admission, query-string parsing, the principal handed to storage, and the
response shape. Storage is a fake that records each ``list_saved_workstreams``
call; the query itself (paging, search, sort, visibility, the lease rule) is
exercised against real databases in ``tests/test_storage_saved_workstreams.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from starlette.responses import JSONResponse

from turnstone.core.session_routes import (
    SessionEndpointConfig,
    make_saved_handler,
    make_unified_saved_handler,
)
from turnstone.core.storage import SavedWorkstreamPage
from turnstone.core.workstream import WorkstreamKind

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

# Async tests run under the project's anyio plugin (mirrors the
# ``@pytest.mark.anyio`` convention in e.g. tests/test_tls_manager.py).
pytestmark = pytest.mark.anyio


def _row(ws_id: str, *, kind: str = "interactive", **overrides: Any) -> dict[str, Any]:
    """One ``list_saved_workstreams`` row."""
    row: dict[str, Any] = {
        "ws_id": ws_id,
        "alias": None,
        "title": None,
        "name": ws_id,
        "created": "2026-01-01T00:00:00",
        "updated": "2026-02-01T00:00:00",
        "message_count": 3,
        "node_id": "node-a",
        "state": "closed",
        "kind": kind,
        "model_alias": "gpt-5",
        "launch_skill": None,
        "child_count": 0,
        "context_tokens": 1000,
        "context_ratio": 0.25,
        "project_id": None,
        "persona": None,
    }
    row.update(overrides)
    return row


class _FakeStorage:
    """Records each saved-list query and answers with a fixed page."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, total: int | None = None):
        self.rows = rows or []
        self.total = len(self.rows) if total is None else total
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def list_saved_workstreams(self, **kwargs: Any) -> SavedWorkstreamPage:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SavedWorkstreamPage(rows=self.rows, total=self.total)


def _request(
    storage: _FakeStorage,
    *,
    query: dict[str, str] | None = None,
    user_id: str = "alice",
    scopes: tuple[str, ...] = ("read",),
    permissions: tuple[str, ...] = (),
) -> Request:
    request = MagicMock()
    request.app.state.auth_storage = storage
    request.query_params = query or {}
    request.state.auth_result = SimpleNamespace(
        user_id=user_id,
        has_scope=lambda scope: scope in scopes,
        has_permission=lambda permission: permission in permissions,
    )
    return request


async def _body(resp: Response) -> dict[str, Any]:
    """Decode a JSONResponse body to a dict."""
    assert isinstance(resp, JSONResponse)
    decoded = json.loads(bytes(resp.body))
    assert isinstance(decoded, dict)
    return decoded


def _coord_cfg(*, permission_gate: Any = None) -> SessionEndpointConfig:
    return SessionEndpointConfig(
        permission_gate=permission_gate,
        manager_lookup=lambda request: (None, None),
        tenant_check=None,
        not_found_label="coordinator not found",
        audit_action_prefix="coordinator",
        list_kind=WorkstreamKind.COORDINATOR,
    )


def _interactive_cfg() -> SessionEndpointConfig:
    return SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda request: (None, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        list_kind=WorkstreamKind.INTERACTIVE,
    )


# ---------------------------------------------------------------------------
# Unified handler — spans both kinds
# ---------------------------------------------------------------------------


async def test_unified_saved_queries_admitted_kinds_as_one_list() -> None:
    """Both kinds go to storage in one query; rows come back in its order with
    the page's total, limit and offset."""
    storage = _FakeStorage(
        [_row("c" * 32, kind="coordinator"), _row("i" * 32, kind="interactive")], total=120
    )
    handler = make_unified_saved_handler([_coord_cfg(), _interactive_cfg()])
    body = await _body(await handler(_request(storage)))

    assert [r["ws_id"] for r in body["workstreams"]] == ["c" * 32, "i" * 32]
    assert (body["total"], body["limit"], body["offset"]) == (120, 50, 0)
    assert storage.calls == [
        {
            "kinds": [WorkstreamKind.COORDINATOR, WorkstreamKind.INTERACTIVE],
            "viewer": "alice",
            "project_names": False,
            "search": "",
            "sort": "updated",
            "descending": True,
            "limit": 50,
            "offset": 0,
        }
    ]


async def test_saved_passes_page_search_and_sort() -> None:
    storage = _FakeStorage()
    query = {"limit": "20", "offset": "40", "q": "  release notes ", "sort": "name"}
    query["order"] = "ASC"
    body = await _body(await make_saved_handler(_interactive_cfg())(_request(storage, query=query)))

    assert (body["limit"], body["offset"]) == (20, 40)
    call = storage.calls[0]
    assert call["search"] == "release notes"
    assert (call["sort"], call["descending"]) == ("name", False)
    assert (call["limit"], call["offset"]) == (20, 40)


@pytest.mark.parametrize(("raw", "limit"), [("500", 200), ("0", 0), ("", 50)])
async def test_saved_clamps_limit(raw: str, limit: int) -> None:
    """A larger page is clamped to the maximum; ``limit=0`` asks for the total."""
    storage = _FakeStorage()
    handler = make_saved_handler(_interactive_cfg())
    body = await _body(await handler(_request(storage, query={"limit": raw})))
    assert body["limit"] == limit
    assert storage.calls[0]["limit"] == limit


@pytest.mark.parametrize(
    "query",
    [
        {"limit": "ten"},
        {"limit": "-3"},
        {"offset": "-1"},
        {"offset": "1.5"},
        {"sort": "state"},
        {"order": "up"},
        {"q": "x" * 257},
        {"q": "x" + chr(0)},
    ],
)
async def test_saved_rejects_malformed_parameters(query: dict[str, str]) -> None:
    """A malformed parameter is a 400, never a silently different page."""
    storage = _FakeStorage()
    handler = make_unified_saved_handler([_coord_cfg(), _interactive_cfg()])
    response = await handler(_request(storage, query=query))
    assert response.status_code == 400
    assert "error" in await _body(response)
    assert storage.calls == []


@pytest.mark.parametrize(
    ("user_id", "scopes", "viewer"),
    [
        ("alice", ("read",), "alice"),
        ("", ("read",), ""),
        ("collector", ("read", "service"), None),
    ],
)
async def test_saved_viewer_follows_the_principal(
    user_id: str, scopes: tuple[str, ...], viewer: str | None
) -> None:
    """Storage filters by the caller's project visibility; only service scope
    reads every row."""
    storage = _FakeStorage()
    handler = make_saved_handler(_interactive_cfg())
    await handler(_request(storage, user_id=user_id, scopes=scopes))
    assert storage.calls[0]["viewer"] == viewer


@pytest.mark.parametrize(
    ("scopes", "permissions", "project_names"),
    [
        (("read",), (), False),
        (("read",), ("project.read",), True),
        (("read", "service"), (), True),
    ],
)
async def test_saved_reads_project_names_only_with_project_read(
    scopes: tuple[str, ...], permissions: tuple[str, ...], project_names: bool
) -> None:
    """Search and the project sort read project names only for a caller who may
    list projects: the dashboards can show the names to no one else."""
    storage = _FakeStorage()
    handler = make_saved_handler(_interactive_cfg())
    await handler(_request(storage, scopes=scopes, permissions=permissions))
    assert storage.calls[0]["project_names"] is project_names


@pytest.mark.parametrize("status", [401, 403, 503])
async def test_per_kind_admission_queries_only_authorized_kinds(status: int) -> None:
    storage = _FakeStorage([_row("interactive")])
    handler = make_unified_saved_handler(
        [
            _coord_cfg(permission_gate=lambda request: JSONResponse({}, status_code=status)),
            _interactive_cfg(),
        ]
    )
    response = await handler(_request(storage))
    if status == 403:
        assert response.status_code == 200
        assert [row["ws_id"] for row in (await _body(response))["workstreams"]] == ["interactive"]
        assert [call["kinds"] for call in storage.calls] == [[WorkstreamKind.INTERACTIVE]]
    else:
        assert response.status_code == status
        assert storage.calls == []


async def test_refusing_every_kind_answers_an_empty_page_without_a_query() -> None:
    """No admitted kind never reaches storage, so no backend can read an empty
    kind list as every kind."""
    storage = _FakeStorage([_row("would-leak")])
    handler = make_unified_saved_handler(
        [_coord_cfg(permission_gate=lambda request: JSONResponse({}, status_code=403))]
    )
    response = await handler(_request(storage))
    assert response.status_code == 200
    assert await _body(response) == {"workstreams": [], "total": 0, "limit": 50, "offset": 0}
    assert storage.calls == []


async def test_excluded_kind_still_validates_configuration() -> None:
    storage = _FakeStorage()
    bad = _coord_cfg(permission_gate=lambda request: JSONResponse({}, status_code=403))
    object.__setattr__(bad, "list_kind", None)
    response = await make_unified_saved_handler([bad, _interactive_cfg()])(_request(storage))
    assert response.status_code == 500
    assert storage.calls == []


async def test_saved_query_failure_is_503_without_detail() -> None:
    storage = _FakeStorage()
    storage.error = RuntimeError("private storage details")
    response = await make_unified_saved_handler([_coord_cfg(), _interactive_cfg()])(
        _request(storage)
    )
    assert response.status_code == 503
    assert await _body(response) == {"error": "Saved sessions unavailable"}


async def test_saved_without_storage_is_503() -> None:
    request = _request(_FakeStorage())
    request.app.state.auth_storage = None
    response = await make_saved_handler(_interactive_cfg())(request)
    assert response.status_code == 503


async def test_unified_saved_500s_on_missing_list_kind() -> None:
    """A cfg without ``list_kind`` is a mount-time misconfig — fail loud
    with 500 rather than filter for the wrong / all kinds."""
    storage = _FakeStorage()
    bad = _interactive_cfg()
    object.__setattr__(bad, "list_kind", None)  # frozen dataclass

    handler = make_unified_saved_handler([_coord_cfg(), bad])
    resp = await handler(_request(storage))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 500
    assert storage.calls == []


# ---------------------------------------------------------------------------
# Single-kind handler and the row shape
# ---------------------------------------------------------------------------


async def test_single_kind_saved_row_shape() -> None:
    """``make_saved_handler`` queries its one kind and serialises each row to
    the saved-list shape, rounding ``context_ratio``."""
    storage = _FakeStorage([_row("c" * 32, kind="coordinator", node_id=None)])
    body = await _body(await make_saved_handler(_coord_cfg())(_request(storage)))

    assert storage.calls[0]["kinds"] == [WorkstreamKind.COORDINATOR]
    row = body["workstreams"][0]
    assert row["context_tokens"] == 1000
    assert row["context_ratio"] == 0.25  # 1000 / 4000
    assert row["node_id"] == ""
    assert row["model_alias"] == "gpt-5"
    assert set(row) == {
        "ws_id",
        "alias",
        "title",
        "name",
        "created",
        "updated",
        "message_count",
        "node_id",
        "state",
        "kind",
        "model_alias",
        "launch_skill",
        "child_count",
        "context_tokens",
        "context_ratio",
        "project_id",
        "persona",
    }


async def test_saved_row_maps_project_and_persona() -> None:
    storage = _FakeStorage(
        [
            _row(
                "c" * 32,
                project_id="proj-x",
                persona="scribe",
                context_tokens=None,
                context_ratio=0,
            )
        ]
    )
    row = (await _body(await make_saved_handler(_interactive_cfg())(_request(storage))))[
        "workstreams"
    ][0]
    assert (row["persona"], row["project_id"]) == ("scribe", "proj-x")
    assert (row["context_tokens"], row["context_ratio"]) == (0, 0.0)


async def test_saved_row_rounds_a_decimal_ratio() -> None:
    """PostgreSQL returns the SQL ratio as a decimal; the payload is a float."""
    from decimal import Decimal

    storage = _FakeStorage([_row("c" * 32, context_ratio=Decimal("0.33333333"))])
    body = await _body(await make_saved_handler(_interactive_cfg())(_request(storage)))
    assert body["workstreams"][0]["context_ratio"] == 0.333


async def test_single_kind_saved_500s_on_missing_list_kind() -> None:
    storage = _FakeStorage()
    bad = _coord_cfg()
    object.__setattr__(bad, "list_kind", None)

    handler = make_saved_handler(bad)
    resp = await handler(_request(storage))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 500
    assert storage.calls == []
