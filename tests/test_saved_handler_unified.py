"""Body-level tests for the saved-workstreams handler factories.

Covers the unified (multi-kind) saved-list handler the console mounts
for its L-shell dashboard, plus a regression guard for the single-kind
:func:`make_saved_handler` after the shared body was lifted into
:func:`turnstone.core.session_routes._collect_saved_rows`.

Storage is mocked (``list_workstreams_with_history`` is patched to
return synthetic 17-tuples) — no real or dev database is touched. The
request is a :class:`unittest.mock.MagicMock`, matching how the
body-level coordinator endpoint tests build request stubs; the saved
path only reads ``request`` to pass it to ``saved_loaded_lookup`` /
``permission_gate``, both of which the tests control.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from starlette.responses import JSONResponse

from turnstone.core.session_routes import (
    SessionEndpointConfig,
    make_saved_handler,
    make_unified_saved_handler,
)
from turnstone.core.workstream import WorkstreamKind

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

# Async tests run under the project's anyio plugin (mirrors the
# ``@pytest.mark.anyio`` convention in e.g. tests/test_tls_manager.py).
pytestmark = pytest.mark.anyio


# Column order from list_workstreams_with_history (keep in sync with the
# storage SELECT): ws_id, alias, title, name, created, updated,
# message_count, node_id, state, kind, model_alias, launch_skill,
# child_count, context_tokens, context_window, project_id, owner.
def _row(
    ws_id: str,
    *,
    updated: Any,
    kind: str,
    state: str = "closed",
    name: str | None = None,
    project_id: str | None = None,
    owner: str | None = None,
) -> tuple[Any, ...]:
    """Build a synthetic storage row (17-tuple) for one workstream."""
    return (
        ws_id,
        None,  # alias
        None,  # title
        name or ws_id,  # name
        "2026-01-01T00:00:00",  # created
        updated,  # updated
        3,  # message_count
        "node-a",  # node_id
        state,  # state
        kind,  # kind
        "gpt-5",  # model_alias
        None,  # launch_skill
        0,  # child_count
        1000,  # context_tokens
        4000,  # context_window
        project_id,  # project_id
        owner,  # owner user_id
    )


def _request() -> Request:
    """A request stub — the saved path only forwards it to the cfg
    callables the tests supply, so a bare MagicMock suffices."""
    return MagicMock()


async def _body(resp: Response) -> dict[str, Any]:
    """Decode a JSONResponse body to a dict."""
    assert isinstance(resp, JSONResponse)
    decoded = json.loads(bytes(resp.body))
    assert isinstance(decoded, dict)
    return decoded


def _coord_cfg(
    *,
    saved_loaded_lookup: Any = None,
    permission_gate: Any = None,
) -> SessionEndpointConfig:
    return SessionEndpointConfig(
        permission_gate=permission_gate,
        manager_lookup=lambda request: (None, None),
        tenant_check=None,
        not_found_label="coordinator not found",
        audit_action_prefix="coordinator",
        list_kind=WorkstreamKind.COORDINATOR,
        saved_state_filter="closed",
        saved_loaded_lookup=saved_loaded_lookup,
    )


def _interactive_cfg() -> SessionEndpointConfig:
    return SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda request: (None, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        list_kind=WorkstreamKind.INTERACTIVE,
        saved_state_filter=None,
        saved_loaded_lookup=None,
    )


def _patch_storage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    coord_rows: list[tuple[Any, ...]],
    interactive_rows: list[tuple[Any, ...]],
) -> list[dict[str, Any]]:
    """Patch ``list_workstreams_with_history`` to dispatch by ``kind``.

    Returns a call-record list so tests can assert each kind queried with
    its own ``state`` filter.
    """
    calls: list[dict[str, Any]] = []

    def _fake(
        limit: int = 20,
        *,
        kind: Any = None,
        user_id: Any = None,
        state: Any = None,
        offset: int = 0,
    ) -> list[tuple[Any, ...]]:
        calls.append({"kind": kind, "state": state, "user_id": user_id, "limit": limit})
        # Honour limit/offset like the real query — the collector pages
        # with OFFSET until it fills its visibility window, so a fake
        # that ignored them would return the same batch forever.
        if kind == WorkstreamKind.COORDINATOR:
            return coord_rows[offset : offset + limit]
        if kind == WorkstreamKind.INTERACTIVE:
            return interactive_rows[offset : offset + limit]
        return []

    # The handler imports the symbol from turnstone.core.memory at call
    # time, so patch it on that module.
    monkeypatch.setattr(
        "turnstone.core.memory.list_workstreams_with_history",
        _fake,
    )
    return calls


# ---------------------------------------------------------------------------
# Unified handler — spans both kinds
# ---------------------------------------------------------------------------


async def test_unified_saved_merges_both_kinds_sorted_by_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unified handler returns coord + interactive rows in one
    ``{"workstreams": [...]}`` response, sorted by ``updated`` desc."""
    coord = [
        _row("c" * 32, updated="2026-03-01T00:00:00", kind="coordinator"),
        _row("d" * 32, updated="2026-01-15T00:00:00", kind="coordinator"),
    ]
    interactive = [
        _row("i" * 32, updated="2026-02-01T00:00:00", kind="interactive", state="active"),
    ]
    _patch_storage(monkeypatch, coord_rows=coord, interactive_rows=interactive)

    handler = make_unified_saved_handler([_coord_cfg(), _interactive_cfg()])
    resp = await handler(_request())
    body = await _body(resp)

    rows = body["workstreams"]
    assert {r["kind"] for r in rows} == {"coordinator", "interactive"}
    assert [r["ws_id"] for r in rows] == ["c" * 32, "i" * 32, "d" * 32]
    # Newest updated first across the merged union.
    assert [r["updated"] for r in rows] == [
        "2026-03-01T00:00:00",
        "2026-02-01T00:00:00",
        "2026-01-15T00:00:00",
    ]


async def test_unified_saved_applies_per_kind_state_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each cfg drives the storage query with its own state filter:
    coord ``"closed"``, interactive ``None``."""
    calls = _patch_storage(monkeypatch, coord_rows=[], interactive_rows=[])

    handler = make_unified_saved_handler([_coord_cfg(), _interactive_cfg()])
    await handler(_request())

    by_kind = {c["kind"]: c for c in calls}
    assert by_kind[WorkstreamKind.COORDINATOR]["state"] == "closed"
    assert by_kind[WorkstreamKind.INTERACTIVE]["state"] is None
    # Cluster-wide visibility on the operator-gated list.
    assert all(c["user_id"] is None for c in calls)


async def test_unified_saved_loaded_lookup_excludes_coord_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coord ``saved_loaded_lookup`` exclusion drops warm-pool coord
    rows but never touches interactive rows (interactive wires None)."""
    coord = [
        _row("c" * 32, updated="2026-03-01T00:00:00", kind="coordinator"),
        _row("e" * 32, updated="2026-02-15T00:00:00", kind="coordinator"),
    ]
    interactive = [
        # Same ws_id stem as an excluded coord id to prove the exclusion
        # is scoped to the coord cfg's rows, not applied globally.
        _row("e" * 32, updated="2026-02-20T00:00:00", kind="interactive", state="active"),
    ]
    _patch_storage(monkeypatch, coord_rows=coord, interactive_rows=interactive)

    async def _loaded(_request: Request) -> set[str]:
        return {"e" * 32}  # warm-pool coord to hide

    handler = make_unified_saved_handler(
        [_coord_cfg(saved_loaded_lookup=_loaded), _interactive_cfg()]
    )
    resp = await handler(_request())
    rows = (await _body(resp))["workstreams"]

    coord_ids = {r["ws_id"] for r in rows if r["kind"] == "coordinator"}
    interactive_ids = {r["ws_id"] for r in rows if r["kind"] == "interactive"}
    assert coord_ids == {"c" * 32}  # warm-pool coord excluded
    assert interactive_ids == {"e" * 32}  # interactive with same id KEPT


async def test_unified_saved_runs_permission_gate_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single ``permission_gate`` short-circuits the whole list; storage
    is never queried when it rejects."""
    calls = _patch_storage(monkeypatch, coord_rows=[], interactive_rows=[])
    reject = JSONResponse({"error": "forbidden"}, status_code=403)

    gate_calls: list[Request] = []

    def _gate(request: Request) -> JSONResponse:
        gate_calls.append(request)
        return reject

    handler = make_unified_saved_handler([_coord_cfg(), _interactive_cfg()], permission_gate=_gate)
    resp = await handler(_request())

    assert resp is reject
    assert len(gate_calls) == 1  # gated once, not per-cfg
    assert calls == []  # no storage query after rejection


async def test_unified_saved_passing_gate_returns_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A passing ``permission_gate`` (returns None) lets the merge through."""
    coord = [_row("c" * 32, updated="2026-03-01T00:00:00", kind="coordinator")]
    _patch_storage(monkeypatch, coord_rows=coord, interactive_rows=[])

    def _gate(_request: Request) -> None:
        return None

    handler = make_unified_saved_handler([_coord_cfg(), _interactive_cfg()], permission_gate=_gate)
    resp = await handler(_request())
    rows = (await _body(resp))["workstreams"]
    assert [r["ws_id"] for r in rows] == ["c" * 32]


async def test_unified_saved_500s_on_missing_list_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cfg without ``list_kind`` is a mount-time misconfig — fail loud
    with 500 rather than filter for the wrong / all kinds."""
    _patch_storage(monkeypatch, coord_rows=[], interactive_rows=[])
    bad = _interactive_cfg()
    object.__setattr__(bad, "list_kind", None)  # frozen dataclass

    handler = make_unified_saved_handler([_coord_cfg(), bad])
    resp = await handler(_request())
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 500


async def test_unified_saved_sorts_none_updated_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows missing ``updated`` sort to the tail under the desc merge."""
    coord = [
        _row("c" * 32, updated=None, kind="coordinator"),
        _row("d" * 32, updated="2026-01-15T00:00:00", kind="coordinator"),
    ]
    interactive = [
        _row("i" * 32, updated="2026-02-01T00:00:00", kind="interactive", state="active"),
    ]
    _patch_storage(monkeypatch, coord_rows=coord, interactive_rows=interactive)

    handler = make_unified_saved_handler([_coord_cfg(), _interactive_cfg()])
    rows = (await _body(await handler(_request())))["workstreams"]
    # Real updated values first (newest first), the None-updated row last.
    assert [r["ws_id"] for r in rows] == ["i" * 32, "d" * 32, "c" * 32]


# ---------------------------------------------------------------------------
# Single-kind handler — regression guard for the body extraction
# ---------------------------------------------------------------------------


async def test_single_kind_saved_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """``make_saved_handler`` still returns ``{"workstreams": [...]}`` for
    one kind, with the loaded-lookup exclusion and row-dict shape intact
    after the shared body was lifted into ``_collect_saved_rows``."""
    coord = [
        _row("c" * 32, updated="2026-03-01T00:00:00", kind="coordinator"),
        _row("e" * 32, updated="2026-02-15T00:00:00", kind="coordinator"),
    ]
    calls = _patch_storage(monkeypatch, coord_rows=coord, interactive_rows=[])

    async def _loaded(_request: Request) -> set[str]:
        return {"e" * 32}

    handler = make_saved_handler(_coord_cfg(saved_loaded_lookup=_loaded))
    resp = await handler(_request())
    body = await _body(resp)

    rows = body["workstreams"]
    assert [r["ws_id"] for r in rows] == ["c" * 32]  # warm-pool row excluded
    # Single-kind query carries the coord state filter.
    assert calls[0]["state"] == "closed"
    assert calls[0]["kind"] == WorkstreamKind.COORDINATOR
    # Row-dict shape + derived context_ratio preserved.
    row = rows[0]
    assert row["context_tokens"] == 1000
    assert row["context_ratio"] == 0.25  # 1000 / 4000
    assert row["node_id"] == "node-a"
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
    }


async def test_single_kind_saved_500s_on_missing_list_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single-kind misconfig guard is unchanged by the extraction."""
    _patch_storage(monkeypatch, coord_rows=[], interactive_rows=[])
    bad = _coord_cfg()
    object.__setattr__(bad, "list_kind", None)

    handler = make_saved_handler(bad)
    resp = await handler(_request())
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 500
