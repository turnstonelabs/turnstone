"""Tests for the coordinator ``/quota`` GET + POST endpoints.

Covers the admin partial-update surface for spawn-budget and
spawn-rate — parallel to the /trust + /restrict shape in
``test_coordinator_governance.py``.  Kept in its own file so PR B's
review surface stays tight.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import (
    _AuthMiddleware,
    _build_mgr,
    _fake_registry,
    _FakeConfigStore,
)
from turnstone.console.server import (
    coordinator_quota_get,
    coordinator_quota_post,
)
from turnstone.core.spawn_quota import SpawnBudget, TokenBucket
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "coord.db"))


_COORD_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"}


def _make_client(storage, *, coord_mgr, alias="my-model", registry=None) -> TestClient:
    app = Starlette(
        routes=[
            Route(
                "/v1/api/coordinator/{ws_id}/quota",
                coordinator_quota_get,
                methods=["GET"],
            ),
            Route(
                "/v1/api/coordinator/{ws_id}/quota",
                coordinator_quota_post,
                methods=["POST"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.coord_mgr = coord_mgr
    app.state.config_store = _FakeConfigStore({"coordinator.model_alias": alias})
    app.state.coord_registry = registry
    app.state.coord_registry_error = "" if coord_mgr else "registry missing"
    app.state.auth_storage = storage
    app.state.jwt_secret = "x" * 64
    return TestClient(app)


def _install_quota(coord) -> tuple[SpawnBudget, TokenBucket]:
    """Attach a real budget + bucket to the coord session under test."""
    budget = SpawnBudget(20)
    bucket = TokenBucket(5.0, 10)
    session = MagicMock()
    session._spawn_budget = budget
    session._spawn_bucket = bucket
    session._coord_client = MagicMock()

    def _get_state():
        return {
            "spawn_budget": budget.budget,
            "spawn_rate": {
                "tokens_per_minute": bucket.tokens_per_minute,
                "burst": bucket.burst,
                "tokens_available": bucket.tokens,
            },
        }

    def _set_budget(n):
        budget.set_budget(int(n))

    def _set_rate(tpm, brst):
        bucket.set_rate(float(tpm), int(brst))

    session.get_quota_state.side_effect = _get_state
    session.set_spawn_budget.side_effect = _set_budget
    session.set_spawn_rate.side_effect = _set_rate
    coord.session = session
    return budget, bucket


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


def test_quota_get_returns_live_snapshot(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/coordinator/{coord.id}/quota",
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["spawn_budget"] == 20
    assert body["spawn_rate"]["tokens_per_minute"] == 5.0
    assert body["spawn_rate"]["burst"] == 10
    assert 0 <= body["spawn_rate"]["tokens_available"] <= 10


def test_quota_get_404_when_session_not_loaded(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = None
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/coordinator/{coord.id}/quota",
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST — happy path
# ---------------------------------------------------------------------------


def test_quota_post_updates_budget_only_and_audits(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    budget, bucket = _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"spawn_budget": 42},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["spawn_budget"] == 42
    # Rate left untouched — the partial update didn't widen it.
    assert body["spawn_rate"]["tokens_per_minute"] == 5.0
    assert body["spawn_rate"]["burst"] == 10
    assert budget.budget == 42

    events = [e for e in storage.list_audit_events() if e["action"] == "coordinator.quota.updated"]
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["before"]["spawn_budget"] == 20
    assert detail["after"]["spawn_budget"] == 42


def test_quota_post_accepts_nested_spawn_rate(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _budget, bucket = _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"spawn_rate": {"tokens_per_minute": 30.0, "burst": 15}},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["spawn_rate"]["tokens_per_minute"] == 30.0
    assert body["spawn_rate"]["burst"] == 15
    assert bucket.burst == 15


def test_quota_post_accepts_flat_aliases(storage):
    """The admin UI may flatten the rate object — both shapes must work."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _budget, bucket = _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"tokens_per_minute": 12.0, "burst": 4},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert bucket.tokens_per_minute == 12.0
    assert bucket.burst == 4


def test_quota_post_burst_only_preserves_refill_rate(storage):
    """Changing only burst shouldn't zero the refill rate — a previous
    bug-prone shape in partial-update handlers that overwrite missing
    fields with defaults.  Here the handler must read current state
    for the missing dimension."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _budget, bucket = _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"burst": 3},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert bucket.tokens_per_minute == 5.0  # unchanged
    assert bucket.burst == 3


def test_quota_post_updates_all_three_knobs_at_once(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    budget, bucket = _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"spawn_budget": 50, "tokens_per_minute": 0.0, "burst": 1},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert budget.budget == 50
    assert bucket.tokens_per_minute == 0.0
    assert bucket.burst == 1


# ---------------------------------------------------------------------------
# POST — validation failures
# ---------------------------------------------------------------------------


def test_quota_post_rejects_empty_body(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_quota_post_rejects_out_of_range_budget(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    for bad in (0, -5, 10_000):
        resp = client.post(
            f"/v1/api/coordinator/{coord.id}/quota",
            json={"spawn_budget": bad},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 400, f"expected 400 for {bad}"


def test_quota_post_rejects_non_numeric_rate(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"tokens_per_minute": "fast"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_quota_post_rejects_out_of_range_rate(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    for bad_tpm in (-1.0, 1_000.0):
        resp = client.post(
            f"/v1/api/coordinator/{coord.id}/quota",
            json={"tokens_per_minute": bad_tpm},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 400


def test_quota_post_rejects_out_of_range_burst(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    for bad in (0, -1, 10_000):
        resp = client.post(
            f"/v1/api/coordinator/{coord.id}/quota",
            json={"burst": bad},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 400


def test_quota_post_rejects_mixed_nested_and_flat_body(storage):
    """Schema description says 'don't mix' — the handler enforces it with 400.

    Silently picking one side would make the admin UI's behaviour
    unpredictable when it accidentally sends both shapes (e.g. during
    a form-rewrite transition).
    """
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"spawn_rate": {"burst": 5}, "burst": 9},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400
    assert "conflicting" in resp.json()["error"]


def test_quota_post_rejects_non_object_spawn_rate(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"spawn_rate": "not-an-object"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_quota_post_rejects_bool_as_numeric_field(storage):
    """``True`` passes ``isinstance(x, int)`` in Python — explicit reject."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    for payload in (
        {"spawn_budget": True},
        {"burst": True},
        {"tokens_per_minute": True},
    ):
        resp = client.post(
            f"/v1/api/coordinator/{coord.id}/quota",
            json=payload,
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 400, f"expected 400 for {payload}"


def test_quota_post_404_when_session_not_loaded(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = None
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"spawn_budget": 5},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 404


def test_quota_post_without_admin_coordinator_is_rejected(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _install_quota(coord)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/quota",
        json={"spawn_budget": 5},
        headers={"X-Test-User": "user-1", "X-Test-Perms": ""},
    )
    assert resp.status_code in (401, 403)
