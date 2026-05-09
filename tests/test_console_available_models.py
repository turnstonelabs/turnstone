"""``GET /v1/api/models`` resolution-chain coverage.

The console handler resolves four defaults from settings + the enabled
model list:

* ``default_alias`` ← ``model.default_alias``
* ``channel_default_alias`` ← ``channels.default_model_alias``
* ``coordinator_default_alias`` ← ``coordinator.model_alias``, falling
  back to ``default_alias`` when empty *or* pointing at a disabled /
  removed alias (mirrors :mod:`turnstone.console.session_factory`).
* ``judge_default_alias`` ← ``judge.model``, falling back to the
  resolved coordinator alias when empty *or* pointing at a value that
  isn't an enabled alias.  ``judge.model`` is alias-only — same
  contract as the other model roles — and
  :class:`turnstone.core.judge.IntentJudge` silently inherits the
  session model when an unknown value is configured, so the API
  surfaces the resolved coordinator alias rather than echoing the
  misconfigured string.

These tests pin each branch so the home composer's resolved-alias
placeholder stays correct as the precedence rules evolve.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _FakeConfigStore
from turnstone.console.server import list_available_models
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "available_models.db"))


def _seed_model(
    storage: SQLiteBackend,
    *,
    definition_id: str,
    alias: str,
    model: str = "model-x",
    enabled: bool = True,
) -> None:
    storage.create_model_definition(
        definition_id=definition_id,
        alias=alias,
        model=model,
        provider="openai-compatible",
        base_url="http://localhost:8000/v1",
        api_key="sk-test",
        context_window=8192,
        capabilities="{}",
        enabled=enabled,
        created_by="admin",
    )


def _make_client(
    storage: SQLiteBackend,
    *,
    settings: dict[str, str] | None = None,
    registry_default: str | None = None,
) -> TestClient:
    app = Starlette(
        routes=[Route("/v1/api/models", list_available_models)],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.config_store = _FakeConfigStore(dict(settings or {}))
    if registry_default is not None:
        # Mimic the live console's ``coord_registry`` shape — the handler
        # only reads ``.default``, so a SimpleNamespace is enough.
        from types import SimpleNamespace

        app.state.coord_registry = SimpleNamespace(default=registry_default)
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": ""})
    return client


def _get_models(client: TestClient) -> dict[str, Any]:
    resp = client.get("/v1/api/models")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Coordinator resolution
# ---------------------------------------------------------------------------


def test_no_settings_leaves_all_defaults_blank(storage: SQLiteBackend) -> None:
    """No model.default_alias, no per-role overrides → every default
    field is empty and ``models`` is an empty list."""
    body = _get_models(_make_client(storage))
    assert body == {
        "models": [],
        "default_alias": "",
        "channel_default_alias": "",
        "coordinator_default_alias": "",
        "judge_default_alias": "",
    }


def test_coordinator_inherits_default_alias_when_unset(
    storage: SQLiteBackend,
) -> None:
    _seed_model(storage, definition_id="m1", alias="primary")
    body = _get_models(_make_client(storage, settings={"model.default_alias": "primary"}))
    assert body["default_alias"] == "primary"
    assert body["coordinator_default_alias"] == "primary"


def test_coordinator_explicit_enabled_alias_passes_through(
    storage: SQLiteBackend,
) -> None:
    _seed_model(storage, definition_id="m1", alias="primary")
    _seed_model(storage, definition_id="m2", alias="fast")
    body = _get_models(
        _make_client(
            storage,
            settings={
                "model.default_alias": "primary",
                "coordinator.model_alias": "fast",
            },
        )
    )
    assert body["coordinator_default_alias"] == "fast"


def test_coordinator_set_to_disabled_alias_falls_back_to_default(
    storage: SQLiteBackend,
) -> None:
    """Operator disabled the alias the coordinator was pinned to —
    fall back to the registry default rather than advertising a model
    that workstream creation would refuse to use."""
    _seed_model(storage, definition_id="m1", alias="primary")
    _seed_model(storage, definition_id="m2", alias="legacy", enabled=False)
    body = _get_models(
        _make_client(
            storage,
            settings={
                "model.default_alias": "primary",
                "coordinator.model_alias": "legacy",
            },
        )
    )
    assert body["coordinator_default_alias"] == "primary"


def test_coordinator_set_to_unknown_alias_falls_back_to_default(
    storage: SQLiteBackend,
) -> None:
    _seed_model(storage, definition_id="m1", alias="primary")
    body = _get_models(
        _make_client(
            storage,
            settings={
                "model.default_alias": "primary",
                "coordinator.model_alias": "ghost",
            },
        )
    )
    assert body["coordinator_default_alias"] == "primary"


def test_coordinator_falls_back_to_registry_default_when_config_store_empty(
    storage: SQLiteBackend,
) -> None:
    """Match ``console/session_factory.py:109-110``: when both
    ``coordinator.model_alias`` and ``model.default_alias`` are unset, new
    coordinator sessions run on ``registry.default`` (loaded from
    config.toml ``[model].default``).  The placeholder must report the
    same alias rather than going blank — otherwise the home composer
    advertises "Default model" while sessions actually launch on a
    concrete alias."""
    _seed_model(storage, definition_id="m1", alias="primary")
    body = _get_models(_make_client(storage, registry_default="primary"))
    assert body["default_alias"] == ""
    assert body["coordinator_default_alias"] == "primary"
    assert body["judge_default_alias"] == "primary"


def test_coordinator_skips_registry_default_when_alias_disabled(
    storage: SQLiteBackend,
) -> None:
    """Registry default points at an alias that's been disabled in the DB
    — the placeholder stays blank rather than advertising a model that
    workstream creation would refuse to use."""
    _seed_model(storage, definition_id="m1", alias="legacy", enabled=False)
    body = _get_models(_make_client(storage, registry_default="legacy"))
    assert body["coordinator_default_alias"] == ""


# ---------------------------------------------------------------------------
# Judge resolution
# ---------------------------------------------------------------------------


def test_judge_empty_inherits_resolved_coordinator_alias(
    storage: SQLiteBackend,
) -> None:
    _seed_model(storage, definition_id="m1", alias="primary")
    _seed_model(storage, definition_id="m2", alias="fast")
    body = _get_models(
        _make_client(
            storage,
            settings={
                "model.default_alias": "primary",
                "coordinator.model_alias": "fast",
            },
        )
    )
    assert body["coordinator_default_alias"] == "fast"
    assert body["judge_default_alias"] == "fast"


def test_judge_explicit_enabled_alias_passes_through(
    storage: SQLiteBackend,
) -> None:
    _seed_model(storage, definition_id="m1", alias="primary")
    _seed_model(storage, definition_id="m2", alias="judge-fast")
    body = _get_models(
        _make_client(
            storage,
            settings={
                "model.default_alias": "primary",
                "judge.model": "judge-fast",
            },
        )
    )
    assert body["judge_default_alias"] == "judge-fast"


def test_judge_set_to_unknown_value_inherits_coordinator(
    storage: SQLiteBackend,
) -> None:
    """``judge.model`` is alias-only — same contract as the other model
    roles.  An unknown value silently inherits the session model in
    :class:`IntentJudge`, so the API surfaces the resolved coordinator
    alias rather than echoing the misconfigured string."""
    _seed_model(storage, definition_id="m1", alias="primary")
    body = _get_models(
        _make_client(
            storage,
            settings={
                "model.default_alias": "primary",
                "judge.model": "anthropic/claude-haiku-4-5",  # raw, not an alias
            },
        )
    )
    assert body["coordinator_default_alias"] == "primary"
    assert body["judge_default_alias"] == "primary"


def test_judge_set_to_disabled_alias_inherits_coordinator(
    storage: SQLiteBackend,
) -> None:
    """Disabled-alias case is handled identically to the unknown-value
    case — both trip the alias-not-resolved path."""
    _seed_model(storage, definition_id="m1", alias="primary")
    _seed_model(storage, definition_id="m2", alias="judge-old", enabled=False)
    body = _get_models(
        _make_client(
            storage,
            settings={
                "model.default_alias": "primary",
                "judge.model": "judge-old",
            },
        )
    )
    assert body["judge_default_alias"] == "primary"


# ---------------------------------------------------------------------------
# Pre-existing fields stay correct under the new resolution code
# ---------------------------------------------------------------------------


def test_channel_default_alias_blanked_when_disabled(
    storage: SQLiteBackend,
) -> None:
    _seed_model(storage, definition_id="m1", alias="primary", enabled=False)
    body = _get_models(
        _make_client(
            storage,
            settings={"channels.default_model_alias": "primary"},
        )
    )
    assert body["channel_default_alias"] == ""


def test_models_payload_strips_secret_fields(storage: SQLiteBackend) -> None:
    """Regression guard: only alias/model/provider land in the response,
    never api_key / base_url / context_window / capabilities."""
    _seed_model(storage, definition_id="m1", alias="primary")
    body = _get_models(_make_client(storage))
    assert body["models"] == [
        {"alias": "primary", "model": "model-x", "provider": "openai-compatible"}
    ]
