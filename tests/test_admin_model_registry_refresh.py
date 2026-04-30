"""Console-side coord_registry auto-refresh on model-definition CRUD + reload.

The console builds ``app.state.coord_registry`` once at lifespan startup
and the coordinator session factory closes over that exact instance.
Without these refresh hooks, an admin who edits a model definition
through the UI sees the DB change immediately but coordinator sessions
keep calling the prior model name — the on-disk truth diverges from the
in-process registry until the console is restarted.

These tests cover both the helper (``_refresh_console_coord_registry``)
and the four wired endpoints (create / update / delete / explicit reload)
to lock in:

- in-place mutation: ``coord_registry`` object identity is preserved
  across refreshes (factory closure must not be invalidated);
- failure isolation: a load or reload failure leaves the existing
  registry intact rather than tearing down a working coordinator;
- no-op safety: the helper short-circuits when ``coord_registry`` is
  ``None`` so a coord-less console (no model rows at boot) doesn't
  500 on routine model-definition CRUD.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

from turnstone.console.server import (
    _refresh_console_coord_registry,
    admin_create_model_definition,
    admin_delete_model_definition,
    admin_model_reload,
    admin_update_model_definition,
)
from turnstone.core.auth import AuthResult
from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "models.db"))


def _seed_model_def(
    storage: SQLiteBackend,
    *,
    definition_id: str,
    alias: str,
    model: str,
    base_url: str = "http://localhost:8000/v1",
    enabled: bool = True,
) -> None:
    """Insert a model definition row directly via the storage API."""
    storage.create_model_definition(
        definition_id=definition_id,
        alias=alias,
        model=model,
        provider="openai-compatible",
        base_url=base_url,
        api_key="sk-test",
        context_window=8192,
        capabilities="{}",
        enabled=enabled,
        created_by="admin",
    )


def _make_registry(*, alias: str = "local", model: str = "old-model") -> ModelRegistry:
    """Build a real ModelRegistry seeded with one alias.

    ModelRegistry.__init__ rejects an empty model dict so tests that
    want to exercise the helper need at least one entry.
    """
    cfg = ModelConfig(
        alias=alias,
        base_url="http://localhost:8000/v1",
        api_key="sk-test",
        model=model,
        context_window=8192,
        provider="openai-compatible",
        source="db",
    )
    return ModelRegistry({alias: cfg}, default=alias)


class _AppState:
    """Shim mirroring Starlette's ``app.state`` for direct helper tests."""

    coord_registry: ModelRegistry | None = None


# ---------------------------------------------------------------------------
# Helper-level tests — ``_refresh_console_coord_registry`` semantics
# ---------------------------------------------------------------------------


def test_helper_rebuilds_registry_from_db(storage: SQLiteBackend) -> None:
    """Helper pulls the latest DB rows into the existing registry."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="new-model")
    state = _AppState()
    state.coord_registry = _make_registry(alias="local", model="old-model")

    _refresh_console_coord_registry(state, storage)

    assert state.coord_registry is not None
    assert state.coord_registry.get_config("local").model == "new-model"


def test_helper_preserves_object_identity(storage: SQLiteBackend) -> None:
    """The factory closes over the registry object — refresh must mutate
    in place rather than swap the attribute."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="new-model")
    state = _AppState()
    state.coord_registry = _make_registry()
    before = id(state.coord_registry)

    _refresh_console_coord_registry(state, storage)

    assert id(state.coord_registry) == before


def test_helper_noop_when_coord_registry_none(storage: SQLiteBackend) -> None:
    """Console boot with no model rows leaves coord_registry = None.
    The helper must not 500 in that state — CRUD that lands the FIRST
    row would otherwise fail before the operator can recover."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    state = _AppState()
    state.coord_registry = None

    _refresh_console_coord_registry(state, storage)  # must not raise

    assert state.coord_registry is None


def test_helper_preserves_registry_when_load_fails(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storage failure during rebuild must not tear down a working
    registry — log + leave the existing instance intact."""
    state = _AppState()
    state.coord_registry = _make_registry(alias="local", model="old-model")

    def _boom(**_kw: Any) -> ModelRegistry:
        raise RuntimeError("simulated storage outage")

    monkeypatch.setattr("turnstone.core.model_registry.load_model_registry", _boom)
    _refresh_console_coord_registry(state, storage)

    assert state.coord_registry is not None
    assert state.coord_registry.get_config("local").model == "old-model"


def test_helper_preserves_registry_when_no_enabled_rows(storage: SQLiteBackend) -> None:
    """All rows disabled/deleted: ModelRegistry.__init__ rejects an empty
    model dict (raises ValueError).  Helper must catch and preserve the
    existing registry so coord stays usable while admin restores rows."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m", enabled=False)
    state = _AppState()
    state.coord_registry = _make_registry(alias="local", model="cached-model")

    _refresh_console_coord_registry(state, storage)

    assert state.coord_registry is not None
    assert state.coord_registry.get_config("local").model == "cached-model"


def test_helper_preserves_registry_on_reload_validation_error(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reload that raises mid-mutation (e.g. validation guard) must
    leave the existing registry instance functional."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="new-model")
    state = _AppState()
    state.coord_registry = _make_registry(alias="local", model="old-model")

    def _broken_reload(*_a: Any, **_kw: Any) -> None:
        raise ValueError("simulated reload validation failure")

    monkeypatch.setattr(state.coord_registry, "reload", _broken_reload)
    _refresh_console_coord_registry(state, storage)

    # Existing registry still reachable; the broken reload was a no-op
    # at the public-facing level.
    assert state.coord_registry is not None
    assert state.coord_registry.get_config("local").model == "old-model"


# ---------------------------------------------------------------------------
# Endpoint-level integration tests — verify wiring
# ---------------------------------------------------------------------------


class _AuthMiddleware(BaseHTTPMiddleware):
    """Inject an admin AuthResult so the permission gate passes."""

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        request.state.auth_result = AuthResult(
            user_id="admin",
            scopes=frozenset({"approve"}),
            token_source="test",
            permissions=frozenset({"admin.models"}),
        )
        return await call_next(request)


def _make_client(storage: SQLiteBackend, registry: ModelRegistry | None) -> TestClient:
    """Build a TestClient wired to the four model-definition endpoints."""
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
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.coord_registry = registry
    # Reload endpoint also touches these — stub them so the test focuses
    # on the registry-refresh behaviour without dragging in a full
    # collector / proxy_client wiring.
    app.state.collector = MagicMock()
    app.state.collector.get_all_nodes.return_value = []
    app.state.proxy_client = MagicMock()
    app.state.config_store = MagicMock()
    return TestClient(app)


def test_create_endpoint_refreshes_registry(storage: SQLiteBackend) -> None:
    """POST /api/admin/model-definitions bumps the in-process registry
    so newly-spawned coord sessions see the new alias immediately."""
    # Pre-existing alias (registry needs at least one row)
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    registry = _make_registry(alias="local", model="m")
    client = _make_client(storage, registry)

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
    assert registry.has_alias("fast")
    assert registry.get_config("fast").model == "fast-model"


def test_update_endpoint_refreshes_registry(storage: SQLiteBackend) -> None:
    """PUT swaps the underlying model name behind a stable alias — the
    user's reported regression."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="old-model")
    registry = _make_registry(alias="local", model="old-model")
    client = _make_client(storage, registry)

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"model": "new-model"},
    )
    assert resp.status_code == 200, resp.text
    assert registry.get_config("local").model == "new-model"


def test_update_endpoint_with_empty_body_does_not_blow_up(
    storage: SQLiteBackend,
) -> None:
    """An empty PUT body skips the storage write (``if updates:``) and
    therefore the registry refresh — registry stays at the prior state.
    Locks the conditional so we don't accidentally do a no-op refresh
    on every PUT (load_model_registry is non-trivial)."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="locked-in")
    registry = _make_registry(alias="local", model="locked-in")
    client = _make_client(storage, registry)

    resp = client.put("/v1/api/admin/model-definitions/m1", json={})
    assert resp.status_code == 200, resp.text
    assert registry.get_config("local").model == "locked-in"


def test_delete_endpoint_refreshes_registry(storage: SQLiteBackend) -> None:
    """DELETE drops the alias from the in-process registry too — a
    coord session that tried to resolve the deleted alias would
    otherwise hit a stale cached client."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    _seed_model_def(storage, definition_id="m2", alias="extra", model="x")
    # Registry seeded with both so the post-delete state is checkable
    cfg_local = ModelConfig(
        alias="local",
        base_url="http://localhost:8000/v1",
        api_key="sk-test",
        model="m",
        context_window=8192,
        provider="openai-compatible",
        source="db",
    )
    cfg_extra = ModelConfig(
        alias="extra",
        base_url="http://localhost:8000/v1",
        api_key="sk-test",
        model="x",
        context_window=8192,
        provider="openai-compatible",
        source="db",
    )
    registry = ModelRegistry({"local": cfg_local, "extra": cfg_extra}, default="local")
    client = _make_client(storage, registry)

    resp = client.delete("/v1/api/admin/model-definitions/m2")
    assert resp.status_code == 200, resp.text
    assert not registry.has_alias("extra")
    assert registry.has_alias("local")  # default alias unaffected


def test_reload_endpoint_refreshes_registry(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit reload button must refresh the console's own
    registry — until this PR it only fanned out to nodes."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="initial")
    registry = _make_registry(alias="local", model="initial")
    client = _make_client(storage, registry)

    # Bypass the CRUD endpoints to mimic an out-of-band DB change (e.g.
    # an operator psql session) and verify the explicit reload path
    # still pulls the change in.
    storage.update_model_definition("m1", model="reloaded-model")

    # Stub the async cluster fan-out helpers — they require a fully-wired
    # collector / proxy_client which is orthogonal to the helper under test.
    async def _noop_publish(_request: Any) -> None:
        return None

    async def _noop_notify(_request: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("turnstone.console.server._publish_config_change", _noop_publish)
    monkeypatch.setattr("turnstone.console.server._notify_nodes_model_reload", _noop_notify)

    resp = client.post("/v1/api/admin/model-definitions/reload")
    assert resp.status_code == 200, resp.text
    assert registry.get_config("local").model == "reloaded-model"
