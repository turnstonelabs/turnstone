"""``models_changed`` SSE fanout coverage.

The console pushes a ``models_changed`` cluster event whenever a model
definition is created / updated / deleted / reloaded, or whenever a
setting in :data:`turnstone.console.server._MODEL_AFFECTING_SETTING_KEYS`
is updated or reset.  Connected browsers refetch ``/v1/api/models`` on
receipt so the home composer dropdown + admin Models → Roles sub-tab
reflect alias edits without a manual reload.

These tests pin two contracts:

- every model-definition CRUD path emits exactly one ``models_changed``
  fanout (so the browser stays in sync with the DB);
- settings PUT / DELETE only emit the fanout when the key is
  model-affecting — unrelated keys (e.g. ``session.retention_days``)
  must not trigger spurious dropdown re-renders across the cluster.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware
from turnstone.console.server import (
    _MODEL_AFFECTING_SETTING_KEYS,
    admin_create_model_definition,
    admin_delete_model_definition,
    admin_delete_setting,
    admin_model_reload,
    admin_update_model_definition,
    admin_update_setting,
)
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "models_changed.db"))


def _seed(storage: SQLiteBackend, *, definition_id: str, alias: str) -> None:
    storage.create_model_definition(
        definition_id=definition_id,
        alias=alias,
        model="model-x",
        provider="openai-compatible",
        base_url="http://localhost:8000/v1",
        api_key="sk-test",
        context_window=8192,
        capabilities="{}",
        enabled=True,
        created_by="admin",
    )


def _make_client(storage: SQLiteBackend) -> tuple[TestClient, MagicMock]:
    """Build a TestClient + return the stub collector for assertion.

    Wires the four model-definition CRUD/reload routes plus the two
    settings mutation routes.  Collector is a MagicMock so each
    ``emit_models_changed`` call lands as a recorded call without
    spinning up the full SSE listener queue.
    """
    app = Starlette(
        routes=[
            Route(
                "/v1/api/admin/model-definitions",
                admin_create_model_definition,
                methods=["POST"],
            ),
            Route(
                "/v1/api/admin/model-definitions/reload",
                admin_model_reload,
                methods=["POST"],
            ),
            Route(
                "/v1/api/admin/model-definitions/{definition_id}",
                admin_update_model_definition,
                methods=["PUT"],
            ),
            Route(
                "/v1/api/admin/model-definitions/{definition_id}",
                admin_delete_model_definition,
                methods=["DELETE"],
            ),
            Route(
                "/v1/api/admin/settings/{key:path}",
                admin_update_setting,
                methods=["PUT"],
            ),
            Route(
                "/v1/api/admin/settings/{key:path}",
                admin_delete_setting,
                methods=["DELETE"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.coord_registry = None  # CRUD endpoints handle this gracefully
    collector = MagicMock()
    collector.get_all_nodes.return_value = []
    app.state.collector = collector
    app.state.proxy_client = MagicMock()
    app.state.config_store = MagicMock()
    client = TestClient(app)
    client.headers.update(
        {
            "X-Test-User": "admin",
            "X-Test-Perms": "admin.models,admin.settings",
        }
    )
    return client, collector


# ---------------------------------------------------------------------------
# Model-definition CRUD endpoints fan out ``models_changed``
# ---------------------------------------------------------------------------


def test_create_emits_models_changed(storage: SQLiteBackend) -> None:
    client, collector = _make_client(storage)
    resp = client.post(
        "/v1/api/admin/model-definitions",
        json={
            "alias": "fast",
            "model": "fast-model",
            "provider": "openai-compatible",
            "base_url": "http://localhost:9000/v1",
            "api_key": "sk-x",
            "context_window": 4096,
        },
    )
    assert resp.status_code == 200, resp.text
    assert collector.emit_models_changed.call_count == 1


def test_update_emits_models_changed(storage: SQLiteBackend) -> None:
    _seed(storage, definition_id="m1", alias="local")
    client, collector = _make_client(storage)
    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"model": "swapped-model"},
    )
    assert resp.status_code == 200, resp.text
    assert collector.emit_models_changed.call_count == 1


def test_update_with_empty_body_does_not_emit(storage: SQLiteBackend) -> None:
    """Empty-body PUT writes no rows + skips the registry refresh — no
    SSE fanout either, since nothing actually changed."""
    _seed(storage, definition_id="m1", alias="local")
    client, collector = _make_client(storage)
    resp = client.put("/v1/api/admin/model-definitions/m1", json={})
    assert resp.status_code == 200, resp.text
    assert collector.emit_models_changed.call_count == 0


def test_delete_emits_models_changed(storage: SQLiteBackend) -> None:
    _seed(storage, definition_id="m1", alias="local")
    client, collector = _make_client(storage)
    resp = client.delete("/v1/api/admin/model-definitions/m1")
    assert resp.status_code == 200, resp.text
    assert collector.emit_models_changed.call_count == 1


def test_reload_emits_models_changed(storage: SQLiteBackend) -> None:
    _seed(storage, definition_id="m1", alias="local")
    client, collector = _make_client(storage)
    resp = client.post("/v1/api/admin/model-definitions/reload")
    assert resp.status_code == 200, resp.text
    assert collector.emit_models_changed.call_count == 1


# ---------------------------------------------------------------------------
# Settings PUT / DELETE only emit for model-affecting keys
# ---------------------------------------------------------------------------


# Pinned snapshot of the role-related keys we expect the allowlist to
# cover today.  The frozenset itself is asserted further down so a
# stray addition doesn't silently bypass coverage.
_EXPECTED_AFFECTING_KEYS = frozenset(
    {
        "model.default_alias",
        "model.plan_alias",
        "model.plan_effort",
        "model.task_alias",
        "model.task_effort",
        "coordinator.model_alias",
        "coordinator.reasoning_effort",
        "judge.model",
    }
)


def _value_for_key(key: str) -> str:
    """Return a registry-valid value for ``key``.

    ``reasoning_effort`` keys have a fixed choice list; alias-shaped
    keys accept arbitrary strings.  Avoids per-key custom payloads.
    """
    if (
        key.endswith("reasoning_effort")
        or key.endswith("plan_effort")
        or key.endswith("task_effort")
    ):
        return "low"
    return "anything"


def test_affecting_keys_set_matches_expected() -> None:
    """Lock in the allowlist so an unintentional removal is caught."""
    assert _MODEL_AFFECTING_SETTING_KEYS == _EXPECTED_AFFECTING_KEYS


@pytest.mark.parametrize("key", sorted(_EXPECTED_AFFECTING_KEYS))
def test_settings_put_emits_for_model_affecting_key(storage: SQLiteBackend, key: str) -> None:
    client, collector = _make_client(storage)
    resp = client.put(
        f"/v1/api/admin/settings/{key}",
        json={"value": _value_for_key(key)},
    )
    assert resp.status_code == 200, resp.text
    assert collector.emit_models_changed.call_count == 1


@pytest.mark.parametrize("key", sorted(_EXPECTED_AFFECTING_KEYS))
def test_settings_delete_emits_for_model_affecting_key(storage: SQLiteBackend, key: str) -> None:
    client, collector = _make_client(storage)
    # Seed a row so DELETE has something to remove (otherwise 404).
    client.put(
        f"/v1/api/admin/settings/{key}",
        json={"value": _value_for_key(key)},
    )
    collector.emit_models_changed.reset_mock()
    resp = client.delete(f"/v1/api/admin/settings/{key}")
    assert resp.status_code == 200, resp.text
    assert collector.emit_models_changed.call_count == 1


def test_settings_put_does_not_emit_for_unrelated_key(
    storage: SQLiteBackend,
) -> None:
    """Updating a non-model setting (here: a session retention knob)
    must not trigger a cluster-wide dropdown refresh."""
    client, collector = _make_client(storage)
    resp = client.put(
        "/v1/api/admin/settings/session.retention_days",
        json={"value": 30},
    )
    assert resp.status_code == 200, resp.text
    assert collector.emit_models_changed.call_count == 0


def test_settings_delete_does_not_emit_for_unrelated_key(
    storage: SQLiteBackend,
) -> None:
    client, collector = _make_client(storage)
    client.put(
        "/v1/api/admin/settings/session.retention_days",
        json={"value": 30},
    )
    collector.emit_models_changed.reset_mock()
    resp = client.delete("/v1/api/admin/settings/session.retention_days")
    assert resp.status_code == 200, resp.text
    assert collector.emit_models_changed.call_count == 0
